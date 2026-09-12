"""The morning brief pipeline.

Shape is MAP-REDUCE, and that is not an optimisation -- it is the only shape that runs.
At num_ctx=8192 a single 21K-token prompt does not fit, and measured on this machine the
7B prefills at ~250 tok/s, so an 8K prompt costs ~30s before a token comes out.

  MAP    : triage mail in batches of 5 on the small model
  REDUCE : synthesise from ~3K tokens of summaries on the large model

Everything that can be arithmetic IS arithmetic. Sections 1 and 6 of the brief never
touch an LLM.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pa_core.connectors.base import CalendarEvent, MailMessage, MailQuery
from pa_core.harness.run import AgentResult, RunContext
from pa_core.llm.base import Task
from pa_core.mailrules import Bucket, RuleConfig, partition, rule_histogram

from .calendar_shape import DayShape, analyse, render_agenda
from .prompts import MAP_SYSTEM, MAP_USER, REDUCE_SYSTEM, REDUCE_USER
from .render import render_brief
from .schema import BriefBody, MailBatch

AGENT = "morning-brief"
MAP_BATCH = 5  # not 40: a 7B model loses track across long batches


def _rule_config(ctx: RunContext, me: str) -> RuleConfig:
    cfg = ctx.cfg
    return RuleConfig(
        internal_domains=frozenset(d.lower() for d in cfg.internal_domains),
        vip_domains=frozenset(_kv_list(ctx, "vip:domains")),
        vip_addrs=frozenset(_kv_list(ctx, "vip:addrs")),
        my_email=me,
        mute_domains=frozenset(_kv_list(ctx, "mute:domains")),
    )


def _kv_list(ctx: RunContext, key: str) -> list[str]:
    from pa_core.store.db import kv_get

    raw = kv_get(ctx.con, key) or ""
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _fmt_mail(m: MailMessage) -> str:
    age_h = int(m.age.total_seconds() // 3600)
    body = (m.body_text or m.snippet or "").strip().replace("\n", " ")[:600]
    return (
        f"--- id:{m.id}\n"
        f"From: {m.from_name or m.from_addr} <{m.from_addr}>\n"
        f"Subject: {m.subject}\n"
        f"Age: {age_h}h | unread: {m.is_unread}\n"
        f"Body: {body}\n"
    )


def _yesterday_status(ctx: RunContext, yday: date) -> str:
    """Resolve yesterday's actions IN CODE, not by asking the model.

    "Still open, 2 days" is the single feature that makes this read like an assistant
    rather than a report generator -- and it is only trustworthy if code checks it.
    """
    rows = ctx.con.execute(
        "select idx, what, thread_id, carry_count from brief_action "
        "where logical_date = ? order by idx",
        (yday.isoformat(),),
    ).fetchall()
    if not rows:
        return "(no brief yesterday)"
    out = []
    for r in rows:
        state = "unknown"
        if r["thread_id"]:
            replied = ctx.con.execute(
                "select 1 from mail_decision where thread_id = ? and received_at > ? limit 1",
                (r["thread_id"], yday.isoformat()),
            ).fetchone()
            state = "activity in that thread since" if replied else "no activity in that thread"
        out.append(
            f"- [{r['idx']}] {r['what']} -- {state}"
            + (f" (carried {r['carry_count']}x)" if r["carry_count"] else "")
        )
    return "\n".join(out)


def _save_actions(ctx: RunContext, body: BriefBody, prev: dict[str, int]) -> None:
    for i, a in enumerate(body.actions, start=1):
        ctx.con.execute(
            """insert into brief_action
               (logical_date, idx, what, why_now, evidence_ref, carry_count)
               values (?,?,?,?,?,?)
               on conflict(logical_date, idx) do update set
                 what=excluded.what, why_now=excluded.why_now,
                 evidence_ref=excluded.evidence_ref, carry_count=excluded.carry_count""",
            (
                ctx.logical_date.isoformat(),
                i,
                a.what,
                a.why_now,
                a.evidence_ref,
                prev.get(a.what.strip().lower(), 0) + (1 if a.carried_from_yesterday else 0),
            ),
        )


def _record_decisions(ctx: RunContext, decided, llm_verdicts: dict[str, Any]) -> None:
    for m, d in decided:
        ctx.con.execute(
            """insert or replace into mail_decision
               (gmail_id, run_id, thread_id, from_addr, from_domain, subject,
                received_at, rule_hit, bucket, confidence, reason)
               values (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                m.id,
                ctx.run_id,
                m.thread_id,
                m.from_addr,
                m.from_domain,
                m.subject,
                m.received_at.isoformat(),
                d.rule,
                str(d.bucket),
                d.confidence,
                d.reason,
            ),
        )
    for m, v in llm_verdicts.values():
        ctx.con.execute(
            """insert or replace into mail_decision
               (gmail_id, run_id, thread_id, from_addr, from_domain, subject,
                received_at, rule_hit, bucket, confidence, reason, model)
               values (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                m.id,
                ctx.run_id,
                m.thread_id,
                m.from_addr,
                m.from_domain,
                m.subject,
                m.received_at.isoformat(),
                None,
                v.bucket,
                None,
                f"{v.timing}: {v.evidence_quote[:120]}".strip(": "),
                ctx.cfg.model_small,
            ),
        )


def build(
    ctx: RunContext,
    *,
    mail_limit: int = 60,
    gmail: Any = None,
    cal: Any = None,
) -> AgentResult:
    """gmail/cal are injectable so the golden test can run the REAL pipeline and the
    REAL model against fake providers, with no Google credentials."""
    cfg = ctx.cfg
    tz = cfg.zone
    today = ctx.logical_date
    tomorrow = today + timedelta(days=1)
    internal = {d.lower() for d in cfg.internal_domains}

    # ---------------------------------------------------------------- fetch
    if gmail is None:
        from pa_core.connectors.gmail import Gmail

        gmail = Gmail()
    if cal is None:
        from pa_core.connectors.gcal import GoogleCalendar

        cal = GoogleCalendar()
    me = cfg.my_email or gmail.profile_email()

    from datetime import datetime

    win_start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
    events: list[CalendarEvent] = cal.list_events(
        start=win_start, end=win_start + timedelta(days=2)
    )

    seen: dict[str, MailMessage] = {}
    for q in (MailQuery.OVERNIGHT, MailQuery.ON_THE_HOOK, MailQuery.IMPORTANT):
        for m in gmail.list_messages(query=q, limit=mail_limit // 2):
            seen.setdefault(m.id, m)
    mails = sorted(seen.values(), key=lambda m: m.received_at, reverse=True)[:mail_limit]
    ctx.log.info("fetched", events=len(events), mails=len(mails))

    # ---------------------------------------------- deterministic: rules + calendar
    rcfg = _rule_config(ctx, me)
    decided, ambiguous = partition(mails, rcfg)
    hist = rule_histogram(decided)

    shape: DayShape = analyse(events, day=today, tz=tz, internal_domains=internal)
    shape_tmr: DayShape = analyse(events, day=tomorrow, tz=tz, internal_domains=internal)

    # -------------------------------------------------------- MAP: small model
    verdicts: dict[str, Any] = {}
    by_id = {m.id: m for m in ambiguous}
    # Bodies only for the few that survived the rules. The rest cost nothing.
    enriched: list[MailMessage] = []
    for m in ambiguous:
        if not m.snippet and not m.body_text:
            try:
                m = gmail.get_message(m.id, include_body=True)
            except Exception as e:
                ctx.log.warning("mail.body_fetch_failed", id=m.id, error=str(e)[:120])
        enriched.append(m)
        by_id[m.id] = m

    def run_map(batch: list[MailMessage], index: int, total: int) -> None:
        res = ctx.llm.complete(
            task=Task.CLASSIFY,
            system=MAP_SYSTEM,
            user=MAP_USER.format(
                chunk="\n".join(_fmt_mail(m) for m in batch), index=index, total=total
            ),
            agent=AGENT,
            run_id=ctx.run_id,
            sensitive=True,  # mail never leaves this machine
            schema=MailBatch,
            max_tokens=1400,
        )
        parsed: MailBatch = res.parsed  # type: ignore[assignment]
        for v in parsed.verdicts:
            if v.msg_id in by_id:
                verdicts[v.msg_id] = (by_id[v.msg_id], v)

    n_batches = max(1, (len(enriched) + MAP_BATCH - 1) // MAP_BATCH)
    for i in range(0, len(enriched), MAP_BATCH):
        run_map(enriched[i : i + MAP_BATCH], i // MAP_BATCH + 1, n_batches)

    # COMPLETENESS GUARD -- measured, not theoretical. Given a batch of 4 the 7B returned
    # ONE verdict and stopped: not truncated (truncated=False, 83 output tokens), just
    # done. A `list[...]` schema with no minimum is satisfied by one element, so
    # constrained decoding cannot catch it -- it guarantees shape, never completeness.
    # Re-asking one message at a time removes the ambiguity about how many answers are
    # expected, and took accuracy on the eval set from 1/4 to 4/4.
    missing = [m for m in enriched if m.id not in verdicts]
    for m in missing[:12]:
        run_map([m], 1, 1)
    unresolved = [m.id for m in enriched if m.id not in verdicts]
    if missing:
        ctx.log.warning("map.backfilled", batched_misses=len(missing), unresolved=len(unresolved))

    # ------------------------------------------------------ REDUCE: large model
    TIMING_WORDS = {
        "hard_deadline": "deadline stated",
        "waiting_on_me": "has asked before / blocked",
        "time_of_day": "tied to today",
        "no_deadline": "no deadline",
        "none": "",
    }

    def why(m: MailMessage, v: Any) -> str:
        """Render the reason from the ENUM plus a quote that code has verified is real.

        This is the grounding guard: a quote the model paraphrased or invented is simply
        not a substring of the message, so it never reaches the page.
        """
        label = TIMING_WORDS.get(v.timing, "")
        q = (v.evidence_quote or "").strip()
        if q:
            hay = f"{m.subject}\n{m.body_text or m.snippet}"

            def norm(t: str) -> str:
                return " ".join(t.split()).lower()

            if norm(q) not in norm(hay):
                ctx.log.warning("map.quote_not_grounded", id=m.id, quote=q[:60])
                q = ""
        return f"{label}: \u201c{q}\u201d" if q and label else (q or label)

    def tri(bucket: str) -> list[str]:
        # The sender address is repeated on every line on purpose: a 7B model given two
        # adjacent lines will otherwise attribute one sender's request to the other.
        return [
            f"- [{v.bucket}] id:{m.id} | from: {v.who} <{m.from_addr}> "
            f"| subject: {m.subject[:70]} | why now: {why(m, v)}"
            for m, v in verdicts.values()
            if v.bucket == bucket
        ]

    summary_block = (
        "\n".join(tri("needs_reply") + tri("needs_attention")) or "(nothing needed a human today)"
    )

    ov = (
        "Overlaps: " + "; ".join(f"{a} vs {b}" for a, b in shape.overlaps) if shape.overlaps else ""
    )
    user = REDUCE_USER.format(
        today=today.isoformat(),
        weekday=today.strftime("%A"),
        agenda="\n".join(render_agenda(shape, tz, internal)) or " (nothing booked)",
        booked=shape.booked_human,
        external=shape.external_count,
        free=" · ".join(b.human(tz) for b in shape.free_blocks) or "none",
        overlaps=ov,
        tomorrow="\n".join(render_agenda(shape_tmr, tz, internal)) or " (nothing booked)",
        n_ambiguous=len(ambiguous),
        n_total=len(mails),
        n_ruled_out=len(decided),
        parts=summary_block,
        yesterday=_yesterday_status(ctx, today - timedelta(days=1)),
        news="(news is not enabled yet)",
    )

    res = ctx.llm.complete(
        task=Task.SYNTHESIZE,
        system=REDUCE_SYSTEM,
        user=user,
        agent=AGENT,
        run_id=ctx.run_id,
        sensitive=True,
        schema=BriefBody,
        max_tokens=2000,
        persona=ctx.persona(),
    )
    body: BriefBody = res.parsed  # type: ignore[assignment]

    # Section 3 is built HERE, in code, from the MAP verdicts -- complete by
    # construction. Anything the rule layer flagged as needing attention (a calendar
    # invite, say) is included too, so a deterministic signal can never be dropped by a
    # model's judgment about how long a list should be.
    from .schema import NeedsYou

    cited = {a.evidence_ref.removeprefix("id:").strip() for a in body.actions if a.evidence_ref}
    rank = {"needs_reply": 0, "needs_attention": 1}
    waiting: list[tuple[int, float, NeedsYou]] = []
    for m, v in verdicts.values():
        if v.bucket not in rank or m.id in cited:
            continue
        waiting.append(
            (
                rank[v.bucket],
                -m.received_at.timestamp(),
                NeedsYou(
                    who=v.who[:60],
                    what=m.subject[:90] or "(no subject)",
                    why_now=why(m, v)[:60],
                    msg_id=m.id,
                ),
            )
        )
    for m, d in decided:
        if d.bucket == Bucket.NEEDS_ATTENTION and m.id not in cited:
            waiting.append(
                (
                    1,
                    -m.received_at.timestamp(),
                    NeedsYou(
                        who=(m.from_name or m.from_addr)[:60],
                        what=m.subject[:90],
                        why_now=d.reason[:60] or d.rule,
                        msg_id=m.id,
                    ),
                )
            )
    waiting.sort(key=lambda t: (t[0], t[1]))
    body.needs_you = [n for _, _, n in waiting][:8]

    # ------------------------------------------------------------- persist + render
    prev = {
        r["what"].strip().lower(): r["carry_count"]
        for r in ctx.con.execute(
            "select what, carry_count from brief_action where logical_date = ?",
            ((today - timedelta(days=1)).isoformat(),),
        )
    }
    ctx.mutate(
        "record mail decisions", lambda: _record_decisions(ctx, decided, verdicts), dry_value=None
    )
    ctx.mutate("save today's actions", lambda: _save_actions(ctx, body, prev), dry_value=None)

    md = render_brief(
        body=body,
        shape=shape,
        shape_tomorrow=shape_tmr,
        tz=tz,
        internal=internal,
        rule_hist=hist,
        n_total=len(mails),
        n_uncertain=len(ambiguous) - len(verdicts),
        generated_at=ctx.now,
        usd=ctx.usage.run_usd(ctx.run_id),
        llm_calls=ctx.con.execute(
            "select count(*) from llm_call where run_id = ?", (ctx.run_id,)
        ).fetchone()[0],
    )
    return AgentResult(
        title=f"Morning Brief — {today:%a %d %b}",
        body_markdown=md,
        metrics={
            "mails_in": float(len(mails)),
            "mails_to_llm": float(len(ambiguous)),
            "ruled_out": float(len(decided)),
            "actions": float(len(body.actions)),
            "meetings": float(len(shape.events)),
        },
    )
