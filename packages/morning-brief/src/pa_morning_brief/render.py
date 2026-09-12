"""Render the brief. Pure function of (LLM body + computed shape) -> markdown.

Being a pure function is what makes the golden test possible, and the golden test is the
only thing that catches "the brief looks wrong" -- which no unit test will.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .calendar_shape import DayShape, render_agenda
from .schema import BriefBody

# Rule name -> how it reads to a human in section 6.
RULE_LABELS = {
    "gmail_category": "promo/social",
    "mailing_list": "lists",
    "noreply_sender": "notifications",
    "otp_or_receipt": "receipts/codes",
    "precedence_bulk": "bulk",
    "auto_submitted": "automated",
    "muted_domain": "muted",
    "from_me": "my own sent mail",
    "calendar_invite": "invites",
}


def render_brief(
    *,
    body: BriefBody,
    shape: DayShape,
    shape_tomorrow: DayShape,
    tz: ZoneInfo,
    internal: set[str],
    rule_hist: dict[str, int],
    n_total: int,
    n_uncertain: int,
    generated_at: datetime,
    usd: float,
    llm_calls: int,
) -> str:
    L: list[str] = []
    local = generated_at.astimezone(tz)
    ruled = sum(rule_hist.values())
    cost = "local" if usd == 0 else f"${usd:.3f}"
    L.append(f"*{local:%H:%M} · mail {n_total}→{n_total - ruled} · {llm_calls} LLM calls · {cost}*")

    # --- 1 ---------------------------------------------------------------
    L.append("\n## 1 · Shape of the day")
    L += render_agenda(shape, tz, internal) or [" (nothing booked)"]
    if shape.free_blocks:
        L.append("Deep-work blocks:  " + " · ".join(b.human(tz) for b in shape.free_blocks))
    else:
        L.append("Deep-work blocks:  **none** — the day is fully booked")
    if shape.focus_blocks:
        L.append("Already defended:  " + " · ".join(b.human(tz) for b in shape.focus_blocks))

    load = f"Load: {shape.booked_human} booked / {shape.external_count} external"
    tmr = f" · TOMORROW: {shape_tomorrow.booked_human} booked"
    if shape_tomorrow.meeting_minutes > max(shape.meeting_minutes, 240):
        tmr += " — protect tonight"
    L.append(load + tmr)
    if shape.overlaps:
        L.append(
            "⚠ Calendar conflicts: "
            + "; ".join(f"**{a}** overlaps **{b}**" for a, b in shape.overlaps)
        )

    # --- 2 ---------------------------------------------------------------
    L.append("\n## 2 · Do these three things")
    if body.actions:
        for i, a in enumerate(body.actions, 1):
            carried = " ↻" if a.carried_from_yesterday else ""
            L.append(f"{i}. {a.what}{carried} — {a.why_now}")
    else:
        L.append("*Nothing forced itself to the top today. That is a real answer.*")

    # --- 3 ---------------------------------------------------------------
    if body.needs_you:
        L.append(f"\n## 3 · Needs you  ({len(body.needs_you)})")
        for n in body.needs_you[:8]:
            L.append(f"· {n.who} — {n.what} — {n.why_now}")

    # --- 4 ---------------------------------------------------------------
    if body.tomorrow_landmine:
        L.append("\n## 4 · Tomorrow's landmine")
        L.append(body.tomorrow_landmine)

    # --- 5 ---------------------------------------------------------------
    if body.news:
        L.append("\n## 5 · Worth 60 seconds")
        for n in body.news:
            L.append(f"· {n.headline} — {n.why_it_matters_to_me} ({n.link_to_my_context})")
            L.append(f"  {n.url}")

    # --- 6: the trust mechanism ------------------------------------------
    L.append("\n## 6 · Handled without you")
    if rule_hist:
        parts = [f"{c} {RULE_LABELS.get(r, r)}" for r, c in rule_hist.items()]
        L.append(f"{ruled} mail ruled out by rules ({' · '.join(parts)})")
    else:
        L.append("0 mail ruled out by rules")
    if shape.skipped:
        L.append(
            "Calendar: "
            + " · ".join(f"{c} {k.replace('_', ' ')}" for k, c in shape.skipped.items())
        )
    if n_uncertain > 0:
        L.append(f"⚠ {n_uncertain} mail the model did not return a verdict for — left alone")
    if body.notes:
        L.append(f"\n{body.notes}")

    return "\n".join(L)
