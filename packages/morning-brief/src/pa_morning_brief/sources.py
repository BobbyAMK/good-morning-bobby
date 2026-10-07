"""Where the tech brief reads from. Every source here was probed live on 2026-09-13.

All $0 and keyless, except CoinMarketCap (free key, optional -- CoinGecko is the default).
Each fetcher returns Items and raises on failure; the agent isolates failures per source,
so one broken feed costs one line in the footer, never the brief.

Deliberately NOT here:
  - ChatGPT release notes (help.openai.com): 403 to non-browsers. OpenAI's news RSS covers
    product launches instead.
  - The community Anthropic RSS feed: lagged the real page by 9+ days when probed, so we
    read anthropic.com/news directly.
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import feedparser
import httpx2 as httpx

from .items import Item, Section, html_to_text

USER_AGENT = "good-morning-bobby/0.1 (personal morning tech brief; one request per source per day)"


@dataclass(frozen=True, slots=True)
class Feed:
    name: str
    url: str
    section: Section
    weight: float = 1.0
    # For personal blogs that mix AI with everything else: keep only AI-related posts.
    require_ai: bool = False


# ------------------------------------------------------------------ source config
# weight = how much a fresh item from this source matters relative to others in its section.
# Official AI sources sit ABOVE the Hacker News ceiling (1 + log10(points) ~ 3.8 at 600
# points): "what did Claude/ChatGPT ship" must lead the section, commentary follows.
FEEDS: list[Feed] = [
    # AI & LLM -- official first
    Feed("OpenAI", "https://openai.com/news/rss.xml", "ai", 5.0),
    Feed("Google DeepMind", "https://deepmind.google/blog/rss.xml", "ai", 4.5),
    Feed("Google AI", "https://blog.google/technology/ai/rss/", "ai", 4.0),
    Feed("Hugging Face", "https://huggingface.co/blog/feed.xml", "ai", 3.0),
    # "everything" includes bird sightings; require_ai keeps only the AI posts.
    Feed(
        "Simon Willison", "https://simonwillison.net/atom/everything/", "ai", 1.2, require_ai=True
    ),
    # General tech
    Feed("TechCrunch", "https://techcrunch.com/feed/", "news", 1.2),
    Feed("The Verge", "https://www.theverge.com/rss/index.xml", "news", 1.0),
    Feed("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab", "news", 1.0),
]

# (owner/repo, weight). GitHub serves an Atom feed per repo; stable releases only.
RELEASE_REPOS: list[tuple[str, float]] = [
    ("anthropics/claude-code", 3.0),
    ("openai/codex", 2.5),
    ("ollama/ollama", 2.0),
    ("astral-sh/uv", 2.0),
    ("huggingface/transformers", 1.8),
    ("pytorch/pytorch", 1.8),
    ("nodejs/node", 1.5),
    ("microsoft/TypeScript", 1.5),
    ("facebook/react", 1.5),
    ("vercel/next.js", 1.3),
    ("rust-lang/rust", 1.3),
]
PYTHON_BLOG = Feed("Python", "https://blog.python.org/feeds/posts/default", "releases", 1.8)

ANTHROPIC_NEWS = "https://www.anthropic.com/news"
CLAUDE_NOTES_MD = "https://docs.claude.com/en/release-notes/overview.md"
CLAUDE_NOTES_URL = "https://platform.claude.com/docs/en/release-notes/overview"
GITHUB_TRENDING = "https://github.com/trending?since=daily"
HN_SEARCH = "https://hn.algolia.com/api/v1/search"

# A Hacker News story about AI belongs in the AI section, not general news.
AI_WORDS = re.compile(
    r"\b(ai|a\.i\.|llms?|gpt[\w.-]*|chatgpt|openai|claude|anthropic|gemini|deepmind|llama|"
    r"mistral|deepseek|qwen|grok|copilot|codex|agents?|transformers?|diffusion|"
    r"hugging ?face|ollama|inference|fine-?tun\w*|embeddings?|frontier( models?)?|"
    r"open[- ](the )?weights?|model weights|dario|altman)\b",
    re.I,
)
# Pre-releases are noise in a daily brief: claude-code alone can tag several a day.
# A release tag must look like a version. pytorch's releases.atom is mostly CI tags --
# trunk/<sha>, viable/strict/<n>, ciflow/xpu/<n> -- measured on 2026-09-13.
VERSION_TAG = re.compile(r"^[A-Za-z_-]*v?\d+(\.\d+){1,3}$")
PRERELEASE = re.compile(r"(?i)(\b|[-_.])(rc\d*|alpha|beta|nightly|canary|preview|dev\d*|pre)\b")
_DATE_WORDS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, \d{4}"


def client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)


def _get(c: httpx.Client, url: str, **kw: Any) -> httpx.Response:
    r = c.get(url, **kw)
    r.raise_for_status()
    return r


def _struct_to_utc(t: Any) -> datetime | None:
    return datetime.fromtimestamp(calendar.timegm(t), tz=UTC) if t else None


def parse_day(s: str) -> datetime:
    """'Sep 10, 2026' / 'September 10, 2026' -> midnight UTC of that day (date_only)."""
    s = s.replace(".", "")
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {s!r}")


# ------------------------------------------------------------------ fetchers
def fetch_feed(c: httpx.Client, feed: Feed) -> list[Item]:
    parsed = feedparser.parse(_get(c, feed.url).content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.bozo_exception}")
    out = []
    for e in parsed.entries:
        link = e.get("link") or ""
        title = html_to_text(e.get("title") or "")
        if not link or not title:
            continue
        body = e.get("summary") or ""
        if not body and e.get("content"):
            body = e.content[0].get("value", "")
        text = html_to_text(body)[:1500]
        if feed.require_ai and not AI_WORDS.search(f"{title} {text[:300]}"):
            continue
        published = _struct_to_utc(e.get("published_parsed") or e.get("updated_parsed"))
        # OpenAI stamps date-only posts as midnight UTC of a day that has not started yet;
        # a future timestamp is a date, not a time ("just now" would be a lie).
        date_only = published is not None and published > datetime.now(UTC)
        out.append(
            Item(
                section=feed.section,
                source=feed.name,
                title=title,
                url=link,
                published=published,
                date_only=date_only,
                text=text,
                weight=feed.weight,
            )
        )
    return out


def fetch_releases(c: httpx.Client, repo: str, weight: float) -> list[Item]:
    items = fetch_feed(
        c, Feed(repo, f"https://github.com/{repo}/releases.atom", "releases", weight)
    )
    name = repo.split("/", 1)[1]
    out = []
    for it in items:
        tag = unquote(it.url.rsplit("/tag/", 1)[-1])
        if not VERSION_TAG.match(tag) or PRERELEASE.search(it.title) or PRERELEASE.search(tag):
            continue
        # Release titles are often just "v2.1.3"; name the project.
        if name.lower() not in it.title.lower():
            it.title = f"{name} {it.title}"
        # GitHub release bodies open with a "What's Changed" heading; it is not content.
        it.text = re.sub(
            r"^(what'?s\s+(new|changed)|changelog|release notes)\s*:?\s*", "", it.text, flags=re.I
        )
        it.meta["repo"] = repo
        out.append(it)
    return out


def fetch_python_releases(c: httpx.Client) -> list[Item]:
    """Python Insider blog. Only posts announcing a stable release."""
    return [
        it
        for it in fetch_feed(c, PYTHON_BLOG)
        if re.search(r"\bPython \d+\.\d+(\.\d+)?\b", it.title)
        and not PRERELEASE.search(it.title)
        and "release candidate" not in it.title.lower()
    ]


def parse_anthropic_news(page: str) -> list[Item]:
    """anthropic.com/news has no feed. Each card is <a href="/news/slug"> holding
    category, date, title and teaser as separate text nodes, in that order."""
    out: dict[str, Item] = {}
    for slug, inner in re.findall(r'<a href="/news/([a-z0-9-]+)"[^>]*>(.*?)</a>', page, re.S):
        parts = [html_to_text(p) for p in re.split(r"<[^>]+>", inner)]
        parts = [p for p in parts if p]
        idx = next((i for i, p in enumerate(parts) if re.fullmatch(_DATE_WORDS, p)), None)
        if idx is None or idx + 1 >= len(parts) or slug in out:
            continue
        out[slug] = Item(
            section="ai",
            source="Anthropic",
            title=parts[idx + 1],
            url=f"https://www.anthropic.com/news/{slug}",
            published=parse_day(parts[idx]),
            date_only=True,
            text=" ".join(parts[idx + 2 : idx + 3])[:600],
            weight=5.0,
        )
    return list(out.values())


def fetch_anthropic_news(c: httpx.Client) -> list[Item]:
    items = parse_anthropic_news(_get(c, ANTHROPIC_NEWS).text)
    if not items:
        raise ValueError("anthropic.com/news parsed to 0 items -- page layout changed?")
    return items


def parse_claude_notes(md: str) -> list[Item]:
    """One Item per dated '### Month D, YYYY' section of the platform release notes."""
    out = []
    sections = re.split(r"^### (" + _DATE_WORDS + r")\s*$", md, flags=re.M)
    for day, body in zip(sections[1::2], sections[2::2], strict=True):
        bullets = [b.strip() for b in re.findall(r"^\s*[*-] (.+)$", body, re.M)]
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", " ".join(bullets)).replace("`", "")
        if not text:
            continue
        published = parse_day(day)
        out.append(
            Item(
                section="ai",
                source="Claude release notes",
                title=f"Claude Platform updates ({published:%b} {published.day})",
                url=CLAUDE_NOTES_URL,
                published=published,
                date_only=True,
                text=text[:1500],
                weight=5.0,
                meta={"key": f"claude-release-notes:{published:%Y-%m-%d}"},
            )
        )
    return out


def fetch_claude_notes(c: httpx.Client) -> list[Item]:
    return parse_claude_notes(_get(c, CLAUDE_NOTES_MD).text)


def _int(s: str) -> int:
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else 0


def parse_trending(page: str, now: datetime) -> list[Item]:
    out = []
    for art in re.split(r'<article class="Box-row', page)[1:]:
        m = re.search(r'href="/([^"/\s]+/[^"/\s]+)/stargazers"[^>]*>(.*?)</a>', art, re.S)
        if not m:
            continue
        repo = m.group(1)
        stars = _int(html_to_text(m.group(2)))
        fm = re.search(r'href="/[^"]+/forks"[^>]*>(.*?)</a>', art, re.S)
        today = re.search(r"([\d,]+) stars? today", art)
        lang = re.search(r'itemprop="programmingLanguage">([^<]+)<', art)
        desc = re.search(r"<p class=\"[^\"]*col-9[^\"]*\"[^>]*>(.*?)</p>", art, re.S)
        out.append(
            Item(
                section="trending",
                source="GitHub trending",
                title=repo,
                url=f"https://github.com/{repo}",
                published=now,  # "trending today" is fresh by definition
                text=html_to_text(desc.group(1)) if desc else "",
                meta={
                    "stars": stars,
                    "forks": _int(html_to_text(fm.group(1))) if fm else 0,
                    "stars_today": _int(today.group(1)) if today else 0,
                    "language": lang.group(1).strip() if lang else "",
                },
            )
        )
    return out


def fetch_trending(c: httpx.Client, now: datetime) -> list[Item]:
    items = parse_trending(_get(c, GITHUB_TRENDING).text, now)
    if not items:
        raise ValueError("github.com/trending parsed to 0 repos -- page layout changed?")
    return items


def fetch_hn(c: httpx.Client, now: datetime, min_points: int = 100) -> list[Item]:
    since = int((now - timedelta(hours=24)).timestamp())
    data = _get(
        c,
        HN_SEARCH,
        params={
            "tags": "story",
            "numericFilters": f"created_at_i>{since},points>{min_points}",
            "hitsPerPage": 60,
        },
    ).json()
    out = []
    for h in data.get("hits", []):
        title = h.get("title") or ""
        hn_url = f"https://news.ycombinator.com/item?id={h['objectID']}"
        points = int(h.get("points") or 0)
        out.append(
            Item(
                section="ai" if AI_WORDS.search(title) else "news",
                source="Hacker News",
                title=title,
                url=h.get("url") or hn_url,
                published=datetime.fromtimestamp(h["created_at_i"], tz=UTC),
                # log scale: 600 points is not 6x as important as 100
                weight=1.0 + math.log10(max(points, 10)),
                meta={
                    "points": points,
                    "comments": int(h.get("num_comments") or 0),
                    "hn_url": hn_url,
                },
            )
        )
    return out


# ------------------------------------------------------------------ crypto
STABLECOINS = frozenset(
    [
        "usdt",
        "usdc",
        "dai",
        "usde",
        "fdusd",
        "tusd",
        "pyusd",
        "usds",
        "busd",
        "usdd",
        "frax",
        "usd1",
        "rlusd",
        "usdtb",
    ]
)


@dataclass(slots=True)
class Coin:
    symbol: str
    name: str
    price: float
    change_24h: float  # percent
    market_cap: float


@dataclass(slots=True)
class CryptoSnapshot:
    provider: str
    coins: list[Coin]  # by market cap, stablecoins removed

    def cap_change(self) -> tuple[float, float]:
        """(total market cap, 24h % change) of these coins, derived from their own caps.

        Not the provider's "global" figure: CoinGecko reported -4.6% for the whole market
        while BTC was -0.8% and ETH -2.3%. A number derived from the coins shown on the
        same line cannot contradict them.
        """
        now = sum(c.market_cap for c in self.coins)
        before = sum(c.market_cap / (1 + c.change_24h / 100) for c in self.coins)
        return now, (now / before - 1) * 100 if before else 0.0


def fetch_coingecko(c: httpx.Client) -> CryptoSnapshot:
    rows = _get(
        c,
        "https://api.coingecko.com/api/v3/coins/markets",
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 100,
            "price_change_percentage": "24h",
        },
    ).json()
    coins = [
        Coin(
            r["symbol"].upper(),
            r["name"],
            float(r["current_price"] or 0),
            float(r.get("price_change_percentage_24h") or 0),
            float(r.get("market_cap") or 0),
        )
        for r in rows
        if r["symbol"].lower() not in STABLECOINS
    ]
    return CryptoSnapshot("CoinGecko", coins)


def fetch_coinmarketcap(c: httpx.Client, key: str) -> CryptoSnapshot:
    h = {"X-CMC_PRO_API_KEY": key, "Accept": "application/json"}
    base = "https://pro-api.coinmarketcap.com"
    rows = _get(c, f"{base}/v1/cryptocurrency/listings/latest", params={"limit": 100}, headers=h)
    coins = [
        Coin(
            r["symbol"].upper(),
            r["name"],
            float(r["quote"]["USD"]["price"] or 0),
            float(r["quote"]["USD"]["percent_change_24h"] or 0),
            float(r["quote"]["USD"]["market_cap"] or 0),
        )
        for r in rows.json().get("data", [])
        if r["symbol"].lower() not in STABLECOINS
    ]
    return CryptoSnapshot("CoinMarketCap", coins)
