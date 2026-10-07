"""Render the tech brief as the small Markdown subset the Telegram sink converts to HTML.

Budget: one Telegram message (4096 chars). ~13 items x ~250 chars + crypto fits; the sink
still splits safely if a long day overflows.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pa_core.text import plain_dashes

from .items import Item, Section, human_count
from .select import Funnel
from .sources import CryptoSnapshot

HEADINGS: dict[Section, str] = {
    "ai": "🤖 AI & LLM",
    "trending": "⭐ GitHub trending",
    "releases": "🛠 Releases",
    "news": "📰 Tech news",
}


def _safe(s: str) -> str:
    """Keep source text from being read as Markdown: brackets would break links, stars
    and backticks would open emphasis or code that never closes."""
    return (
        plain_dashes(s)
        .replace("[", "(")
        .replace("]", ")")
        .replace("*", "")
        .replace("`", "'")
        .strip()
    )


def _age(it: Item, now: datetime) -> str:
    if it.published is None:
        return ""
    if it.date_only:
        return f"{it.published:%b} {it.published.day}"
    h = int(max(0.0, (now - it.published).total_seconds()) // 3600)
    return "just now" if h == 0 else f"{h}h ago"


def _line(it: Item, now: datetime, n: int) -> str:
    """One numbered item. The number is what a "reply 3 7" save refers to."""
    link = f"{n}. [{_safe(it.title)}]({it.url})"
    if it.section == "trending":
        m = it.meta
        bits = [
            f"★ {human_count(m.get('stars', 0))} (+{human_count(m.get('stars_today', 0))} today)"
        ]
        if m.get("forks"):
            bits.append(f"{human_count(m['forks'])} forks")
        if m.get("language"):
            bits.append(m["language"])
        desc = _safe(it.text)
        desc = desc if len(desc) <= 140 else desc[:139].rsplit(" ", 1)[0] + "…"
        return f"{link} - {desc + ' · ' if desc else ''}{' · '.join(bits)}"

    out = link
    if it.summary:
        out += f" - {_safe(it.summary)}"
    tail = [s for s in (it.source if it.source != "Hacker News" else "", _age(it, now)) if s]
    if it.also:
        tail.append("also " + ", ".join(it.also[:2]))
    if "points" in it.meta:
        tail.append(f"HN {it.meta['points']} pts · {it.meta.get('comments', 0)} comments")
    if tail:
        out += f" _({' · '.join(tail)})_"
    return out


def _usd(x: float) -> str:
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    return f"${x:.4g}"


def _pct(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}{abs(x):.1f}%"


def _big(x: float) -> str:
    return f"${x / 1e12:.2f}T" if x >= 1e12 else f"${x / 1e9:.0f}B"


def render_crypto(snap: CryptoSnapshot) -> list[str]:
    top = snap.coins[:5]
    L = [" · ".join(f"**{c.symbol}** {_usd(c.price)} ({_pct(c.change_24h)})" for c in top)]
    ranked = sorted(snap.coins, key=lambda c: -c.change_24h)
    up = [c for c in ranked[:3] if c.change_24h > 0]
    down = [c for c in ranked[::-1][:2] if c.change_24h < 0]
    if up or down:
        movers = [f"▲ {c.symbol} {_pct(c.change_24h)}" for c in up]
        movers += [f"▼ {c.symbol} {_pct(c.change_24h)}" for c in down]
        L.append("Movers: " + " · ".join(movers))
    cap, change = snap.cap_change()
    L.append(
        f"Top {len(snap.coins)} coins {_big(cap)} ({_pct(change)}) _({snap.provider}, stablecoins excluded)_"
    )
    return L


def numbered(chosen: dict[Section, list[Item]]) -> list[tuple[Item, int]]:
    """Section order, then rank order, numbered 1..N. Render and storage both use this, so
    the number on screen and the number in the DB cannot drift apart."""
    flat = [it for s in HEADINGS for it in chosen.get(s) or []]
    return [(it, i) for i, it in enumerate(flat, start=1)]


def render_tech_brief(
    *,
    chosen: dict[Section, list[Item]],
    crypto: CryptoSnapshot | None,
    funnel: Funnel,
    now: datetime,
    tz: ZoneInfo,
    llm_calls: int,
) -> str:
    n = sum(len(v) for v in chosen.values())
    L = [
        f"*{now.astimezone(tz):%H:%M} · {funnel.scanned} scanned → {n} new · last 24h · {llm_calls} LLM calls · local*"
    ]

    for it, num in numbered(chosen):
        heading = HEADINGS[it.section]
        if f"\n## {heading}" not in L:
            L.append(f"\n## {heading}")
        L.append(_line(it, now, num))

    if n:
        L.append("\n_Reply with numbers to save them as a pinned note, e.g. 3 7_")
    else:
        L.append("\n*Nothing new in the last 24 hours that has not already been sent.*")

    if crypto and crypto.coins:
        L.append("\n## 💰 Crypto · 24h")
        L += render_crypto(crypto)

    L.append("\n## Filtered")
    parts = [f"{funnel.scanned} scanned", f"{funnel.too_old} older than 24h"]
    if funnel.undated:
        parts.append(f"{funnel.undated} undated")
    parts += [f"{funnel.merged} same story", f"{funnel.already_sent} already sent"]
    L.append(" · ".join(parts))
    if funnel.failed_sources:
        L.append("⚠ sources failed: " + ", ".join(sorted(funnel.failed_sources)))
    return "\n".join(L)
