"""The two promises of the tech brief -- last 24h, never repeated -- live in pure code."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pa_morning_brief.items import Item, canonical_url, same_title, title_tokens
from pa_morning_brief.select import drop_already_sent, is_fresh, merge_same_story, pick, score
from pa_morning_brief.sources import (
    VERSION_TAG,
    Coin,
    CryptoSnapshot,
    parse_anthropic_news,
    parse_claude_notes,
    parse_trending,
)
from pa_morning_brief.summarize import first_sentence, grounded

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 13, 23, 0, tzinfo=UTC)


def item(title="A thing happened today in tech", url="https://x.com/a", section="news", **kw):
    kw.setdefault("published", NOW - timedelta(hours=2))
    return Item(section=section, source=kw.pop("source", "Src"), title=title, url=url, **kw)


# ---------------------------------------------------------------- identity
def test_canonical_url_ignores_tracking_www_fragment_and_slash():
    a = canonical_url("http://www.Example.com/post/?utm_source=hn&id=3#comments")
    b = canonical_url("https://example.com/post?id=3")
    assert a == b


def test_canonical_url_keeps_meaningful_query():
    assert canonical_url("https://x.com/p?id=1") != canonical_url("https://x.com/p?id=2")


def test_same_title_catches_rewording_but_not_short_titles():
    a = title_tokens("OpenAI launches GPT-6 Astra for enterprise customers")
    b = title_tokens("GPT-6 Astra launches for OpenAI enterprise customers today")
    assert same_title(a, b)
    assert not same_title(title_tokens("GPT-6"), title_tokens("GPT-6"))
    assert not same_title(a, title_tokens("Nvidia is the central bank of AI"))


# ---------------------------------------------------------------- freshness
def test_timed_item_window_is_exactly_24h():
    assert is_fresh(item(published=NOW - timedelta(hours=23, minutes=59)), NOW)
    assert not is_fresh(item(published=NOW - timedelta(hours=24, minutes=1)), NOW)


def test_undated_item_is_neither_fresh_nor_old():
    assert is_fresh(item(published=None), NOW) is None


def test_date_only_counts_by_day():
    day = lambda d: datetime(2026, 9, d, tzinfo=UTC)
    assert is_fresh(item(published=day(12), date_only=True), NOW)  # window starts on the 12th
    assert not is_fresh(item(published=day(11), date_only=True), NOW)


# ---------------------------------------------------------------- dedup
def test_hn_copy_of_a_story_merges_and_donates_its_points():
    rss = item(url="https://techcrunch.com/x?utm_source=rss", source="TechCrunch", weight=5.0)
    hn = item(url="https://techcrunch.com/x", source="Hacker News", meta={"points": 612})
    for it in (rss, hn):
        it.score = score(it, NOW)
    kept, merged = merge_same_story([hn, rss])
    assert merged == 1 and len(kept) == 1
    assert kept[0].source == "TechCrunch" and kept[0].meta["points"] == 612
    assert kept[0].also == ["Hacker News"]


def test_trending_repos_never_merge_on_similar_names():
    a = item("owner/fast-json-parser-rust", "https://github.com/a/x", section="trending")
    b = item("other/fast-json-parser-rust", "https://github.com/b/x", section="trending")
    kept, merged = merge_same_story([a, b])
    assert merged == 0 and len(kept) == 2


def test_already_sent_by_key_or_by_retitled_story():
    same_url = item(url="https://x.com/a")
    retitled = item("Anthropic releases Claude Opus 5 with longer context", url="https://y.com/b")
    new = item("Something else entirely different happened", url="https://z.com/c")
    history = [
        (canonical_url("https://x.com/a"), "whatever"),
        ("https://q.com/q", "Claude Opus 5 released by Anthropic with longer context"),
    ]
    kept, dropped = drop_already_sent([same_url, retitled, new], history)
    assert kept == [new] and dropped == 2


def test_release_notes_for_a_new_day_are_not_a_duplicate():
    md = "### September 12, 2026\n\n* Added a thing\n\n### September 11, 2026\n\n* Older thing\n"
    notes = parse_claude_notes(md)
    assert len(notes) == 2 and notes[0].url == notes[1].url
    assert notes[0].key != notes[1].key  # same page URL, different day -> different item
    kept, _ = drop_already_sent(notes[:1], [(notes[1].key, notes[1].title)])
    assert kept == notes[:1]


# ---------------------------------------------------------------- selection
def test_pick_respects_quotas_and_one_release_per_project():
    items = [
        item(
            f"claude-code v2.1.{n} release",
            f"https://g.com/{n}",
            section="releases",
            meta={"repo": "anthropics/claude-code"},
            score=10 - n,
        )
        for n in range(3)
    ] + [
        item(f"AI story number {n} here", f"https://a.com/{n}", section="ai", score=5)
        for n in range(20)
    ]
    chosen = pick(items)
    assert len(chosen["releases"]) == 1
    assert len(chosen["ai"]) == 4 + 3  # quota, plus spare slots capped at +3
    assert sum(len(v) for v in chosen.values()) <= 15


def test_version_tags_only():
    assert VERSION_TAG.match("v2.1.270") and VERSION_TAG.match("1.98.1")
    for tag in ("trunk/4d2bd99", "viable/strict/1789", "ciflow/xpu/196712"):
        assert not VERSION_TAG.match(tag)


# ---------------------------------------------------------------- parsers (real HTML)
def test_parse_trending_real_markup():
    repos = parse_trending((FIX / "github_trending.html").read_text(), NOW)
    assert len(repos) == 2
    r = repos[0]
    assert "/" in r.title and r.url == f"https://github.com/{r.title}"
    assert r.meta["stars"] > r.meta["stars_today"] > 0 and r.meta["forks"] > 0


def test_parse_anthropic_news_real_markup():
    news = parse_anthropic_news((FIX / "anthropic_news.html").read_text())
    assert len(news) == 2
    assert all(
        n.date_only and n.published and n.title and "anthropic.com/news/" in n.url for n in news
    )


# ---------------------------------------------------------------- summaries + crypto
def test_grounded_rejects_invented_numbers():
    src = "uv 0.9.2 adds lockfile pruning and is 3x faster on large workspaces."
    assert grounded("uv 0.9.2 prunes lockfiles and is 3x faster.", src)
    assert not grounded("uv 1.0 prunes lockfiles and is 3x faster.", src)


def test_first_sentence_truncates_on_a_word():
    s = first_sentence("word " * 100, limit=40)
    assert len(s) <= 40 and s.endswith("…")


def test_market_cap_change_is_derived_from_the_coins_shown():
    snap = CryptoSnapshot("t", [Coin("BTC", "b", 1, -1.0, 600.0), Coin("ETH", "e", 1, -3.0, 400.0)])
    cap, change = snap.cap_change()
    assert cap == 1000.0 and -3.0 < change < -1.0
