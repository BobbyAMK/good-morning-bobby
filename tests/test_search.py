"""/search: understanding, candidate query, enrichment joins, HN filter, ranking."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pa_core.store import db, migrate
from pa_morning_brief import search as S

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


class Log:
    def __getattr__(self, _):
        return lambda *a, **k: None


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data = status, data or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None):
        self.calls.append(url)
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp(url, params) if callable(resp) else resp
        return Resp(404)


class FakeLLM:
    def __init__(self, parsed=None, exc=None):
        self.parsed, self.exc = parsed, exc

    def complete(self, **kw):
        if self.exc:
            raise self.exc
        return type("R", (), {"parsed": self.parsed})()


def repo(name, stars, pushed_days=1, created_days=900, **kw):
    return S.Repo(
        full_name=name,
        url=f"https://github.com/{name}",
        description="",
        stars=stars,
        forks=0,
        language="",
        pushed_at=NOW - timedelta(days=pushed_days),
        created_at=NOW - timedelta(days=created_days),
        **kw,
    )


# ---------------------------------------------------------------- understanding
def test_normalize_keywords():
    assert S.normalize_keywords(["ReactJS", "Animation library", "react", "lint"]) == [
        "react",
        "animation",
        "linter",
    ]


def test_fallback_from_a_vietnamese_sentence():
    i = S.fallback_intent("tìm cho tôi các ReactJS Animation library đang hot nhất hiện nay")
    assert i.keywords == ["react", "animation"]


def test_model_failure_falls_back_and_code_sets_ecosystem_and_ranking():
    intent, how = S.understand(
        FakeLLM(exc=TimeoutError()), "ReactJS animation library đang hot nhất hiện nay", Log()
    )
    assert how == "fallback" and intent.ecosystem == "javascript" and intent.ranking == "hot"


def test_model_ranking_is_overruled_by_the_words_of_the_request():
    parsed = S.SearchIntent(
        is_software_search=True, keywords=["lint", "python"], ecosystem="python", ranking="new"
    )
    intent, _ = S.understand(FakeLLM(parsed), "công cụ lint Python nhanh", Log())
    assert intent.ranking == "popular" and intent.keywords == ["linter", "python"]


# ---------------------------------------------------------------- GitHub query
def test_python_is_a_keyword_not_a_language_filter():
    q = S.github_query(
        S.SearchIntent(is_software_search=True, ecosystem="python", ranking="popular"),
        ["linter"],
        NOW,
    )
    assert "language:" not in q and q.startswith("linter python ")


def test_hot_requires_recent_activity():
    q = S.github_query(S.SearchIntent(is_software_search=True, ranking="hot"), ["react"], NOW)
    assert "pushed:>2026-06-15" in q


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("git+https://github.com/MotionDivision/motion.git", "motiondivision/motion"),
        ("https://github.com/pmndrs/react-spring/tree/main/packages/web", "pmndrs/react-spring"),
        ("git@github.com:astral-sh/ruff.git", "astral-sh/ruff"),
        ("https://gitlab.com/a/b", None),
        (None, None),
    ],
)
def test_repo_slug(url, slug):
    assert S.repo_slug(url) == slug


# ---------------------------------------------------------------- enrichment
def test_npm_takes_the_biggest_named_package_not_a_sum():
    r = repo("motiondivision/motion", 34000)
    links = lambda n: {
        "package": {
            "name": n,
            "links": {"repository": "git+https://github.com/motiondivision/motion.git"},
        }
    }
    downloads = {"framer-motion": 34_000_000, "motion": 15_000_000}
    http = FakeHttp(
        {
            "https://registry.npmjs.org/-/v1/search": Resp(
                data={"objects": [links("framer-motion"), links("motion")]}
            ),
            "https://api.npmjs.org/downloads/point/last-week/": lambda u, p: Resp(
                data={"downloads": downloads[u.rsplit("/", 1)[1]]}
            ),
        }
    )
    S.enrich_npm(http, [r], ["react", "animation"])
    assert (r.downloads_week, r.download_package) == (34_000_000, "framer-motion")


def test_pypi_package_with_the_same_name_but_another_repo_is_rejected():
    r = repo("someone/requests", 10)
    http = FakeHttp(
        {
            "https://pypi.org/pypi/requests/json": Resp(
                data={"info": {"project_urls": {"Source": "https://github.com/psf/requests"}}}
            ),
            "https://pypistats.org": Resp(data={"data": {"last_week": 270_000_000}}),
        }
    )
    S.enrich_pypi(http, [r])
    assert r.downloads_week == 0 and not any("pypistats" in c for c in http.calls)


# ---------------------------------------------------------------- HN + ranking
def test_hn_title_must_name_every_keyword():
    assert S.hn_title_matches("Show HN: A React animation toolkit", ["react", "animation"])
    assert not S.hn_title_matches(
        "Show HN: I replaced a $120k bowling center system", ["react", "animation"]
    )
    assert not S.hn_title_matches("Reactive animation in Rust", ["react", "animation"])


def test_rank_downloads_and_activity_beat_raw_stars():
    stale = repo("a/stale", 40000, pushed_days=800)
    alive = repo("b/alive", 20000, pushed_days=2, downloads_week=5_000_000, download_source="npm")
    assert [r.full_name for r in S.rank([stale, alive], "popular", NOW)] == ["b/alive", "a/stale"]


# ---------------------------------------------------------------- run
@pytest.fixture
def con():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    migrate.migrate(c, db.MIGRATIONS)
    return c


class Chat:
    def __init__(self):
        self.sent = []

    def send_message(self, md, *, reply_to=None, notify=False):
        self.sent.append(md)
        return [4242]

    def pin(self, _):
        pass


def test_general_question_is_declined_without_touching_github(con):
    parsed = S.SearchIntent(is_software_search=False)
    http = FakeHttp({})
    out = S.run(
        Chat(),
        con,
        llm=FakeLLM(parsed),
        http=http,
        query="giá vàng hôm nay",
        user_message_id=1,
        now=NOW,
        log=Log(),
    )
    assert out == "not_software" and http.calls == []


def test_results_are_numbered_and_stored_for_saving(con):
    parsed = S.SearchIntent(
        is_software_search=True, keywords=["react", "animation"], ecosystem="any"
    )
    item = {
        "full_name": "pmndrs/react-spring",
        "html_url": "https://github.com/pmndrs/react-spring",
        "description": "spring physics",
        "stargazers_count": 29000,
        "forks_count": 1,
        "language": "TypeScript",
        "pushed_at": "2026-09-10T00:00:00Z",
        "created_at": "2018-01-01T00:00:00Z",
    }
    http = FakeHttp(
        {S.GITHUB: Resp(data={"items": [item]}), "https://hn.algolia.com": Resp(data={"hits": []})}
    )
    chat = Chat()
    out = S.run(
        chat,
        con,
        llm=FakeLLM(parsed),
        http=http,
        query="react animation",
        user_message_id=1,
        now=NOW,
        log=Log(),
    )
    assert out == "results" and "1. [pmndrs/react-spring]" in chat.sent[0]
    row = con.execute("select message_id, n, url from search_result").fetchone()
    assert (row["message_id"], row["n"], row["url"]) == (
        4242,
        1,
        "https://github.com/pmndrs/react-spring",
    )
