"""The morning tech brief: what happened in tech in the last 24 hours, never repeated.

  fetch (parallel, each source isolated)  ->  fresh within 24h  ->  merge same story
  ->  drop anything an earlier brief sent  ->  quotas per section  ->  summarise (local 7B,
  grounded)  ->  crypto snapshot  ->  render  ->  remember what was sent

Only the summaries touch a model. Freshness, dedup and selection are code.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from pa_core import secrets as sec
from pa_core.errors import PaError
from pa_core.harness.run import AgentResult, RunContext

from . import sources as S
from .items import Item
from .select import Funnel, drop_already_sent, is_fresh, merge_same_story, pick, score
from .summarize import summarize
from .tech_render import numbered, render_tech_brief

AGENT = "morning-brief"
DEDUP_DAYS = 30


class NoSourcesReachable(PaError):
    """Every source failed -- almost certainly no network. Fail loudly, do not send an
    empty brief that looks like a quiet day."""


def _jobs(c: Any, now: datetime) -> dict[str, Callable[[], list[Item]]]:
    jobs: dict[str, Callable[[], list[Item]]] = {
        f.name: partial(S.fetch_feed, c, f) for f in S.FEEDS
    }
    jobs |= {repo: partial(S.fetch_releases, c, repo, w) for repo, w in S.RELEASE_REPOS}
    jobs["Python"] = partial(S.fetch_python_releases, c)
    jobs["Anthropic"] = partial(S.fetch_anthropic_news, c)
    jobs["Claude release notes"] = partial(S.fetch_claude_notes, c)
    jobs["GitHub trending"] = partial(S.fetch_trending, c, now)
    jobs["Hacker News"] = partial(S.fetch_hn, c, now)
    return jobs


def fetch_all(ctx: RunContext, c: Any, now: datetime, funnel: Funnel) -> list[Item]:
    raw: list[Item] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fn): name for name, fn in _jobs(c, now).items()}
        for f in as_completed(futures):
            name = futures[f]
            try:
                got = f.result()
                raw += got
                ctx.log.debug("source.ok", source=name, items=len(got))
            except Exception as e:
                funnel.failed_sources.append(name)
                ctx.log.warning(
                    "source.failed", source=name, error=f"{type(e).__name__}: {e}"[:200]
                )
    return raw


def fetch_crypto(ctx: RunContext, c: Any, funnel: Funnel) -> S.CryptoSnapshot | None:
    key = sec.get_optional("coinmarketcap_key")
    try:
        return S.fetch_coinmarketcap(c, key) if key else S.fetch_coingecko(c)
    except Exception as e:
        funnel.failed_sources.append("crypto")
        ctx.log.warning("source.failed", source="crypto", error=f"{type(e).__name__}: {e}"[:200])
        return None


def _remember(ctx: RunContext, items: list[tuple[Item, int]]) -> None:
    day = ctx.logical_date.isoformat()
    # A same-day --force re-run replaces today's rows wholesale; otherwise items dropped
    # from the new selection would keep their old numbers and "reply 3" could hit two.
    ctx.con.execute("delete from sent_item where sent_on = ?", (day,))
    for it, n in items:
        ctx.con.execute(
            """insert or replace into sent_item (url_key, title, section, url, sent_on, n, summary)
               values (?,?,?,?,?,?,?)""",
            (it.key, it.title, it.section, it.url, day, n, it.summary or it.text[:200]),
        )


def build(ctx: RunContext, *, http: Any = None, now: datetime | None = None) -> AgentResult:
    now = (now or ctx.now).astimezone(UTC)
    today = ctx.logical_date
    funnel = Funnel()
    c = http or S.client()

    raw = fetch_all(ctx, c, now, funnel)
    crypto = fetch_crypto(ctx, c, funnel)
    funnel.scanned = len(raw)
    if not raw:
        raise NoSourcesReachable(f"all {len(funnel.failed_sources)} sources failed")

    fresh: list[Item] = []
    for it in raw:
        ok = is_fresh(it, now)
        if ok is None:
            funnel.undated += 1
        elif not ok:
            funnel.too_old += 1
        else:
            it.score = score(it, now)
            fresh.append(it)

    fresh, funnel.merged = merge_same_story(fresh)
    # EARLIER days only: a same-day `--force` re-run must reproduce today's brief, not
    # come back empty because this morning's run already "sent" everything.
    history = ctx.con.execute(
        "select url_key, title from sent_item where sent_on < ? and sent_on >= ?",
        (today.isoformat(), (today - timedelta(days=DEDUP_DAYS)).isoformat()),
    ).fetchall()
    fresh, funnel.already_sent = drop_already_sent(fresh, [(r[0], r[1]) for r in history])

    chosen = pick(fresh)
    flat = [it for items in chosen.values() for it in items]
    stats = summarize(ctx, [it for it in flat if it.section != "trending"])

    md = render_tech_brief(
        chosen=chosen,
        crypto=crypto,
        funnel=funnel,
        now=now,
        tz=ctx.cfg.zone,
        llm_calls=ctx.con.execute(
            "select count(*) from llm_call where run_id = ?", (ctx.run_id,)
        ).fetchone()[0],
    )
    ctx.mutate("remember sent items", lambda: _remember(ctx, numbered(chosen)), dry_value=None)

    return AgentResult(
        title=f"Tech Brief - {today:%a %d %b}",
        body_markdown=md,
        metrics={
            "scanned": float(funnel.scanned),
            "too_old": float(funnel.too_old),
            "merged": float(funnel.merged),
            "already_sent": float(funnel.already_sent),
            "items": float(len(flat)),
            "failed_sources": float(len(funnel.failed_sources)),
            **{k: float(v) for k, v in stats.items()},
        },
    )
