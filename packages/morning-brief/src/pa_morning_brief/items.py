"""One news item, and the pure functions that decide identity: canonical URL + title key.

"Never send the same story twice" is only as good as these two functions, which is why
they are pure, small, and tested.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

Section = Literal["ai", "trending", "releases", "news"]

_TRACKING = re.compile(r"^(utm_\w+|ref|ref_src|fbclid|gclid|mc_cid|mc_eid|source|via|s)$", re.I)
_STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "your",
        "you",
        "our",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "this",
        "that",
        "these",
        "those",
        "its",
        "it's",
        "how",
        "why",
        "what",
        "when",
        "who",
        "new",
        "now",
        "just",
        "more",
        "than",
        "over",
        "about",
        "after",
        "before",
        "will",
        "can",
        "not",
        "but",
        "all",
        "out",
        "get",
        "got",
        "use",
        "using",
        "via",
    ]
)


@dataclass(slots=True)
class Item:
    section: Section
    source: str  # human label, e.g. "OpenAI", "Hacker News"
    title: str
    url: str
    published: datetime | None  # tz-aware UTC; None = the source gave no date
    date_only: bool = False  # source publishes a day, not a time
    text: str = ""  # plain text the source provided -- the ONLY input to a summary
    weight: float = 1.0  # source/project importance, set by the source config
    score: float = 0.0
    summary: str = ""
    also: list[str] = field(default_factory=list)  # other sources carrying the same story
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Identity for dedup. Sources whose URL is not unique per item set meta['key']."""
        return self.meta.get("key") or canonical_url(self.url)

    @property
    def title_dedup(self) -> bool:
        """Whether a similar TITLE may mark this item as a duplicate.

        Not for trending repos (similar names, different projects), and not for items
        with an explicit key: "Claude Platform updates (Sep 12)" and "(Sep 11)" share every
        content word, so title matching silently dropped each new day's release notes.
        """
        return self.section != "trending" and "key" not in self.meta

    @property
    def title_tokens(self) -> frozenset[str]:
        return title_tokens(self.title)


def canonical_url(url: str) -> str:
    """Same article, same key: https, no www, no fragment, no tracking params, no trailing /."""
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    if p.port and p.port not in (80, 443):
        host = f"{host}:{p.port}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)))
    path = p.path.rstrip("/") or ""
    return urlunsplit(("https", host, path, query, ""))


def title_tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[\w][\w.+#-]*", title.lower())
    return frozenset(w.strip(".-") for w in words if len(w) >= 3 and w not in _STOP)


def same_title(a: frozenset[str], b: frozenset[str], threshold: float = 0.6) -> bool:
    """Jaccard on content words. Needs 3+ words on both sides, or 'GPT-6' matches 'GPT-6'."""
    if len(a) < 3 or len(b) < 3:
        return False
    return len(a & b) / len(a | b) >= threshold


def html_to_text(s: str) -> str:
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def human_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)
