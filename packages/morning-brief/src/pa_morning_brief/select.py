"""From a few hundred raw items to 10-15: freshness, same-story merge, history dedup, quotas.

All of it is arithmetic. The model never decides what is new, what is a duplicate, or what
makes the cut -- those are the two promises of this brief ("last 24h", "no repeats"), and a
promise a 7B model is in charge of is not a promise.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .items import Item, Section, same_title

WINDOW = timedelta(hours=24)
QUOTAS: dict[Section, int] = {"ai": 4, "trending": 3, "releases": 3, "news": 3}
MAX_ITEMS = 15
# Spare slots (a quiet section) go to these sections, in this order.
SPILL_ORDER: tuple[Section, ...] = ("ai", "news", "releases", "trending")


@dataclass(slots=True)
class Funnel:
    """Counts for the footer. A filter that does not say what it dropped is not trusted."""

    scanned: int = 0
    too_old: int = 0
    undated: int = 0
    merged: int = 0
    already_sent: int = 0
    failed_sources: list[str] = field(default_factory=list)


def is_fresh(item: Item, now: datetime, window: timedelta = WINDOW) -> bool | None:
    """True/False, or None when the source gave no date (we cannot prove it is fresh).

    Date-only sources (a page that says 'Sep 10, 2026') count as fresh when that day is on
    or after the day the window starts: they can be up to ~48h old at the edge, and the
    history dedup is what stops them from appearing twice.
    """
    if item.published is None:
        return None
    start = now - window
    if item.date_only:
        return item.published.date() >= start.date()
    return item.published >= start  # feeds sometimes stamp the future; that is still fresh


def score(item: Item, now: datetime) -> float:
    if item.section == "trending":
        return float(item.meta.get("stars_today", 0))
    s = item.weight
    if item.published is not None and not item.date_only:
        age_h = max(0.0, (now - item.published).total_seconds() / 3600)
        s += max(0.0, 1.0 - age_h / 24)  # up to +1 for brand new
    return s


def merge_same_story(items: list[Item]) -> tuple[list[Item], int]:
    """One story, many sources -> one item. The highest-scoring copy wins; a Hacker News
    copy donates its points and comment count, so the kept item still shows the signal."""
    ordered = sorted(items, key=lambda i: -i.score)
    kept: list[Item] = []
    merged = 0
    for it in ordered:
        twin = next(
            (
                k
                for k in kept
                if k.key == it.key
                or (
                    k.title_dedup and it.title_dedup and same_title(k.title_tokens, it.title_tokens)
                )
            ),
            None,
        )
        if twin is None:
            kept.append(it)
            continue
        merged += 1
        if it.source not in twin.also and it.source != twin.source:
            twin.also.append(it.source)
        for k in ("points", "comments", "hn_url"):
            if k in it.meta and k not in twin.meta:
                twin.meta[k] = it.meta[k]
    return kept, merged


def drop_already_sent(
    items: list[Item], history: Iterable[tuple[str, str]]
) -> tuple[list[Item], int]:
    """history: (url_key, title) of items carried by briefs on EARLIER days.

    Same key, or a near-identical title (the same launch re-reported by another outlet
    tomorrow), counts as already sent.
    """
    from .items import title_tokens

    keys: set[str] = set()
    titles: list[frozenset[str]] = []
    for k, t in history:
        keys.add(k)
        titles.append(title_tokens(t))
    out = []
    for it in items:
        if it.key in keys or (
            it.title_dedup and any(same_title(it.title_tokens, t) for t in titles)
        ):
            continue
        out.append(it)
    return out, len(items) - len(out)


def pick(items: list[Item]) -> dict[Section, list[Item]]:
    """Best items per section within quotas; releases are one per project."""
    by: dict[Section, list[Item]] = {s: [] for s in QUOTAS}
    for it in sorted(items, key=lambda i: -i.score):
        by[it.section].append(it)

    seen_repos: set[str] = set()
    releases = []
    for it in by["releases"]:
        repo = it.meta.get("repo") or it.source
        if repo in seen_repos:
            continue
        seen_repos.add(repo)
        releases.append(it)
    by["releases"] = releases

    chosen = {s: by[s][: QUOTAS[s]] for s in QUOTAS}
    spare = MAX_ITEMS - sum(len(v) for v in chosen.values())
    for s in SPILL_ORDER:
        while spare > 0 and len(chosen[s]) < min(len(by[s]), QUOTAS[s] + 3):
            chosen[s].append(by[s][len(chosen[s])])
            spare -= 1
    return chosen
