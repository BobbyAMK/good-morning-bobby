"""/search <natural language> -> top libraries, repos and tools. $0, keyless.

  understand (local 7B -> keywords/ecosystem/ranking, code fallback)
  -> GitHub repository search (the candidate set)
  -> enrich: npm weekly downloads (JS) / pypistats (Python), joined by repository URL
  -> rank in code -> Hacker News discussions whose title names every keyword -> render

Measured on 2026-09-13 before this was written:
  - GitHub search for "react animation" returned motion, react-spring, auto-animate: good.
  - npm search for the same returned react-smooth first -- 22.8M downloads/week because
    recharts depends on it. So npm never decides WHAT is a candidate, only adds a signal
    to repositories GitHub already found.
  - Hacker News search for "react animation" returned a 2935-point post about a bowling
    alley. So an HN hit must name every keyword in its title.
"""

from __future__ import annotations

import math
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pa_core.llm.base import Task
from pydantic import BaseModel, Field

from .items import human_count
from .notes import Chat
from .tech_render import _safe

Ecosystem = Literal["javascript", "python", "any"]
Ranking = Literal["hot", "popular", "new"]

GITHUB = "https://api.github.com/search/repositories"
TOP_N = 8


# ------------------------------------------------------------------ understanding
class SearchIntent(BaseModel):
    model_config = {"extra": "forbid"}
    is_software_search: bool = Field(
        description="true if the user wants software: libraries, frameworks, tools, repositories, packages"
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="1-4 short ENGLISH technical terms that appear in repository names, descriptions or topics",
    )
    ecosystem: Ecosystem = "any"
    ranking: Ranking = "popular"


SYSTEM = """\
You turn a developer's search request into a GitHub repository search. Requests are often
in Vietnamese. Return JSON only.

is_software_search: true when they want software -- a library, framework, tool,
  repository or package. false for news, prices, people, how-to questions, or anything else.
keywords: 1-4 short ENGLISH technical terms that would appear in a repository name,
  description or topics. Translate Vietnamese ("thư viện vẽ biểu đồ" -> "chart").
  Leave out words that only describe the request: library, tool, best, top, hot, popular,
  new, fast, "thư viện", "công cụ", "hiện nay", "nhanh".
  Use the noun a repository description would use: "linter" not "lint", "client" not
  "request".
ecosystem: javascript for React, Vue, Node, TypeScript, npm; python for Python, Django,
  FastAPI, pandas, PyPI; otherwise any.
ranking: hot for "hot", "trending", "đang hot", "hiện nay", "nổi bật"; new for "mới",
  "mới ra", "recently released"; otherwise popular."""

SYNONYMS = {
    "reactjs": "react",
    "react.js": "react",
    "nodejs": "node",
    "node.js": "node",
    "vuejs": "vue",
    "vue.js": "vue",
    "next.js": "nextjs",
    "js": "javascript",
    "ts": "typescript",
    # GitHub search does not stem: "lint" does not match "linter" in a description.
    "lint": "linter",
    "format": "formatter",
    "orm": "orm",
}
FILLER = frozenset(
    [
        "library",
        "libraries",
        "lib",
        "libs",
        "tool",
        "tools",
        "framework",
        "frameworks",
        "package",
        "packages",
        "best",
        "top",
        "hot",
        "popular",
        "new",
        "newest",
        "latest",
        "trending",
        "open-source",
        "opensource",
        "repo",
        "repos",
        "github",
        "module",
        "modules",
    ]
)
# ASCII leftovers of Vietnamese sentences ("tìm cho tôi các" -> "cho"), for the fallback.
VI_ASCII_STOP = frozenset(
    [
        "cho",
        "toi",
        "minh",
        "cac",
        "nhung",
        "la",
        "va",
        "de",
        "trong",
        "hien",
        "nay",
        "dang",
        "nhat",
        "tim",
        "giup",
        "voi",
        "ve",
        "co",
        "mot",
        "nao",
        "gi",
        "the",
        "nhu",
        "duoc",
        "khong",
        "hay",
        "xin",
        "ban",
        "hot",
        "top",
    ]
)
JS_HINT = re.compile(
    r"\b(react(\.?js)?|vue(\.?js)?|svelte|angular|node(\.?js)?|typescript|javascript|npm|"
    r"next\.?js|deno|bun)\b",
    re.I,
)
PY_HINT = re.compile(r"\b(python|django|flask|fastapi|pandas|numpy|pypi|pytorch|pydantic)\b", re.I)
HOT_HINT = re.compile(r"(hot|trending|hiện nay|nổi bật|thịnh hành)", re.I)
NEW_HINT = re.compile(r"(\bmới\b|mới ra|recently|newest|\bnew\b)", re.I)


def normalize_keywords(words: list[str]) -> list[str]:
    out: list[str] = []
    for w in words:
        for token in w.lower().split():
            token = SYNONYMS.get(token.strip(".,;:!?\"'()"), token.strip(".,;:!?\"'()"))
            if token and token not in FILLER and token not in out:
                out.append(token)
    return out[:4]


def fallback_intent(query: str) -> SearchIntent:
    """No model: ASCII words minus filler and Vietnamese leftovers. Tech terms are English
    even in a Vietnamese sentence, so this is a real answer, not a placeholder."""
    # Whole whitespace tokens that are entirely ASCII. A character regex chopped "tìm" into
    # "t" and "m" (caught by a test; the live run never showed it because the model worked).
    words = [
        w
        for w in (t.strip(".,;:!?\"'()") for t in query.split())
        if w and w.isascii() and w[0].isalpha() and w.lower() not in VI_ASCII_STOP
    ]
    return SearchIntent(
        is_software_search=True,
        keywords=normalize_keywords(words),
        ecosystem="any",
        ranking="popular",
    )


def understand(llm: Any, query: str, log: Any) -> tuple[SearchIntent, str]:
    """Returns (intent, how) where how is 'model' or 'fallback'. Code has the last word on
    ecosystem and ranking whenever the request says so explicitly."""
    intent, how = None, "model"
    try:
        res = llm.complete(
            task=Task.EXTRACT,
            system=SYSTEM,
            user=f"Request: {query}",
            agent="bot",
            run_id=None,
            schema=SearchIntent,
            max_tokens=200,
        )
        intent = res.parsed
        intent.keywords = normalize_keywords(intent.keywords)
        if intent.is_software_search and not intent.keywords:
            intent = None
    except Exception as e:
        log.warning("search.intent_failed", error=f"{type(e).__name__}: {e}"[:200])
    if intent is None:
        intent, how = fallback_intent(query), "fallback"
    if JS_HINT.search(query):
        intent.ecosystem = "javascript"
    elif PY_HINT.search(query):
        intent.ecosystem = "python"
    # Ranking comes from the words in the request, never from the model: asked for a
    # "fast" (nhanh) Python linter, the 7B chose `new` and the search returned only repos
    # created in the last six months.
    intent.ranking = (
        "hot" if HOT_HINT.search(query) else "new" if NEW_HINT.search(query) else "popular"
    )
    return intent, how


# ------------------------------------------------------------------ candidates
@dataclass(slots=True)
class Repo:
    full_name: str
    url: str
    description: str
    stars: int
    forks: int
    language: str
    pushed_at: datetime
    created_at: datetime
    downloads_week: int = 0
    download_source: str = ""  # "npm" | "PyPI"
    download_package: str = ""  # the package the number belongs to -- always shown
    packages: list[str] = field(default_factory=list)
    score: float = 0.0


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def github_query(intent: SearchIntent, keywords: list[str], now: datetime) -> str:
    # Python is a KEYWORD, never `language:python`: ruff and uv are written in Rust, and the
    # language filter dropped ruff from "fast Python linter" entirely (measured).
    words = list(keywords)
    if intent.ecosystem == "python" and "python" not in words:
        words.append("python")
    q = " ".join(words) + " in:name,description,topics archived:false"
    if intent.ranking == "hot":
        q += f" stars:>100 pushed:>{(now - timedelta(days=90)):%Y-%m-%d}"
    elif intent.ranking == "new":
        q += f" stars:>20 created:>{(now - timedelta(days=180)):%Y-%m-%d}"
    else:
        q += " stars:>300"
    return q


def search_github(http: Any, intent: SearchIntent, now: datetime, token: str | None) -> list[Repo]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    kws = intent.keywords
    for attempt in (kws, kws[:2]):  # relax once: 4 keywords can over-constrain
        r = http.get(
            GITHUB,
            params={
                "q": github_query(intent, attempt, now),
                "sort": "stars",
                "order": "desc",
                "per_page": 20,
            },
            headers=headers,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if items or len(attempt) <= 2:
            break
    return [
        Repo(
            full_name=i["full_name"],
            url=i["html_url"],
            description=(i.get("description") or "").strip(),
            stars=int(i.get("stargazers_count") or 0),
            forks=int(i.get("forks_count") or 0),
            language=i.get("language") or "",
            pushed_at=_dt(i["pushed_at"]),
            created_at=_dt(i["created_at"]),
        )
        for i in items
    ]


_GH_REPO = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?]|$)", re.I)


def repo_slug(url: str | None) -> str | None:
    """'git+https://github.com/MotionDivision/motion.git' -> 'motiondivision/motion'."""
    m = _GH_REPO.search(url or "")
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def enrich_npm(http: Any, repos: list[Repo], keywords: list[str]) -> None:
    by_slug = {r.full_name.lower(): r for r in repos}
    res = http.get(
        "https://registry.npmjs.org/-/v1/search", params={"text": " ".join(keywords), "size": 100}
    )
    res.raise_for_status()
    for obj in res.json().get("objects", []):
        pkg = obj.get("package", {})
        slug = repo_slug((pkg.get("links") or {}).get("repository"))
        if slug in by_slug and pkg.get("name") not in by_slug[slug].packages:
            by_slug[slug].packages.append(pkg["name"])

    # Repos npm search did not surface: try the repository name itself as a package,
    # accepted only if that package points back at the same repository.
    def by_repo_name(r: Repo) -> None:
        name = r.full_name.split("/", 1)[1].lower()
        meta = http.get(f"https://registry.npmjs.org/{name}")
        if meta.status_code == 200:
            repo_field = meta.json().get("repository")
            url = repo_field.get("url") if isinstance(repo_field, dict) else repo_field
            if repo_slug(url) == r.full_name.lower():
                r.packages.append(name)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(by_repo_name, [r for r in repos[:TOP_N] if not r.packages]))

    def weekly(name: str) -> int:
        d = http.get(f"https://api.npmjs.org/downloads/point/last-week/{name}")
        return int(d.json().get("downloads") or 0) if d.status_code == 200 else 0

    pairs = [(r, name) for r in repos for name in r.packages[:5]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(lambda p: weekly(p[1]), pairs))
    # The BIGGEST package, named -- not a sum. Measured: pmndrs/react-three-fiber matched
    # only the retired `react-three-fiber` package (25k/wk) while the live one is
    # `@react-three/fiber`; an unnamed "25k/wk" would have read as the project's size.
    for (r, name), count in zip(pairs, counts, strict=True):
        if count > r.downloads_week:
            r.downloads_week, r.download_source, r.download_package = count, "npm", name


def enrich_pypi(http: Any, repos: list[Repo]) -> None:
    # Parallel: pypistats answers in ~2-4s per package; sequentially a Python search
    # measured 52s end to end.
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda r: _pypi_one(http, r), repos[: TOP_N + 4]))


def _pypi_one(http: Any, r: Repo) -> None:
    name = r.full_name.split("/", 1)[1]
    meta = http.get(f"https://pypi.org/pypi/{name}/json")
    if meta.status_code != 200:
        return
    info = meta.json().get("info", {})
    urls = [*(info.get("project_urls") or {}).values(), info.get("home_page") or ""]
    if r.full_name.lower() not in {repo_slug(u) for u in urls}:
        return  # same name, different project: never borrow its downloads
    stats = http.get(f"https://pypistats.org/api/packages/{name.lower()}/recent")
    if stats.status_code == 200:
        r.downloads_week = int(stats.json().get("data", {}).get("last_week") or 0)
        r.download_source = "PyPI"
        r.download_package = name
        r.packages.append(name)


def rank(repos: list[Repo], ranking: Ranking, now: datetime) -> list[Repo]:
    for r in repos:
        s = math.log10(r.stars + 1)
        if r.downloads_week:
            s += 0.6 * math.log10(r.downloads_week + 1)
        pushed_days = (now - r.pushed_at).days
        if pushed_days <= 30:
            s += 0.5
        elif pushed_days > 365:
            s -= 1.0
        if ranking == "new":
            s += 2.0 * max(0.0, 1.0 - (now - r.created_at).days / 180)
        r.score = s
    return sorted(repos, key=lambda r: -r.score)


# ------------------------------------------------------------------ discussions
@dataclass(slots=True)
class Discussion:
    title: str
    url: str
    points: int
    created_at: datetime


def hn_title_matches(title: str, keywords: list[str]) -> bool:
    low = title.lower()
    return all(re.search(rf"(?<![\w]){re.escape(k)}(?![\w])", low) for k in keywords)


def search_hn(http: Any, keywords: list[str], now: datetime) -> list[Discussion]:
    r = http.get(
        "https://hn.algolia.com/api/v1/search",
        params={
            "query": " ".join(keywords),
            "tags": "story",
            "numericFilters": f"created_at_i>{int((now - timedelta(days=365)).timestamp())},points>50",
            "hitsPerPage": 40,
        },
    )
    r.raise_for_status()
    hits = [
        Discussion(
            h.get("title") or "",
            f"https://news.ycombinator.com/item?id={h['objectID']}",
            int(h.get("points") or 0),
            datetime.fromtimestamp(h["created_at_i"], tz=UTC),
        )
        for h in r.json().get("hits", [])
        if hn_title_matches(h.get("title") or "", keywords)
    ]
    return sorted(hits, key=lambda d: -d.points)[:3]


# ------------------------------------------------------------------ render + run
def _ago(dt: datetime, now: datetime) -> str:
    days = (now - dt).days
    if days <= 0:
        return "today"
    if days < 60:
        return f"{days}d ago"
    return f"{days // 30}mo ago"


def render(
    query_label: str, intent: SearchIntent, repos: list[Repo], hn: list[Discussion], now: datetime
) -> tuple[str, list[tuple[int, str, str, str]]]:
    """Markdown plus the numbered rows (n, title, url, line_md) that /search stores."""
    eco = {"javascript": "JavaScript", "python": "Python", "any": "all languages"}[intent.ecosystem]
    L = [
        f"🔎 **{_safe(query_label)}** · {intent.ranking} · {eco}",
        "_GitHub search, ranked by stars + downloads + recent activity_",
        "",
    ]
    rows: list[tuple[int, str, str, str]] = []
    n = 0
    for r in repos:
        n += 1
        desc = _safe(r.description)
        desc = desc if len(desc) <= 110 else desc[:109].rsplit(" ", 1)[0] + "…"
        bits = [f"★{human_count(r.stars)}"]
        if r.downloads_week:
            bits.append(
                f"{human_count(r.downloads_week)}/wk {r.download_source} {r.download_package}"
            )
        bits.append(f"pushed {_ago(r.pushed_at, now)}")
        if r.language:
            bits.append(r.language)
        body = (
            f"[{r.full_name}]({r.url})" + (f" - {desc}" if desc else "") + " · " + " · ".join(bits)
        )
        L.append(f"{n}. {body}")
        rows.append((n, r.full_name, r.url, body))
    if hn:
        L.append("\n**Discussed on Hacker News**")
        for d in hn:
            n += 1
            body = f"[{_safe(d.title)}]({d.url}) - {d.points} pts · {d.created_at:%b %Y}"
            L.append(f"{n}. {body}")
            rows.append((n, d.title, d.url, body))
    L.append("\n_Reply with numbers to save them as a pinned note_")
    return "\n".join(L), rows


def run(
    chat: Chat,
    con: sqlite3.Connection,
    *,
    llm: Any,
    http: Any,
    query: str,
    user_message_id: int,
    now: datetime,
    log: Any,
    github_token: str | None = None,
) -> str:
    query = query.strip()
    if not query:
        chat.send_message(
            "Cách dùng: `/search <điều bạn cần tìm>`\n"
            "Ví dụ: `/search ReactJS animation library đang hot nhất hiện nay`",
            reply_to=user_message_id,
        )
        return "usage"

    intent, how = understand(llm, query, log)
    log.info("search.intent", how=how, **intent.model_dump())
    if not intent.is_software_search:
        chat.send_message(
            "⚠ /search chỉ tìm **thư viện, framework, tool, repo** (GitHub, npm, PyPI, Hacker News). "
            "Câu hỏi web tổng quát như tin tức hay giá cả thì chưa hỗ trợ.",
            reply_to=user_message_id,
        )
        return "not_software"

    repos = search_github(http, intent, now, github_token)
    label = " ".join(intent.keywords)
    if not repos:
        chat.send_message(
            f"Không tìm thấy repo nào trên GitHub cho: `{label}`. Thử từ khóa tiếng Anh khác, "
            "ví dụ `/search react chart`.",
            reply_to=user_message_id,
        )
        return "no_results"

    def enrich() -> None:
        try:
            if intent.ecosystem == "javascript":
                enrich_npm(http, repos, intent.keywords)
            elif intent.ecosystem == "python":
                enrich_pypi(http, repos)
        except Exception as e:  # downloads are a bonus signal, never a failure
            log.warning("search.enrich_failed", error=f"{type(e).__name__}: {e}"[:200])

    def discussions() -> list[Discussion]:
        try:
            return search_hn(http, intent.keywords, now)
        except Exception as e:
            log.warning("search.hn_failed", error=f"{type(e).__name__}: {e}"[:200])
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_enrich, f_hn = pool.submit(enrich), pool.submit(discussions)
        f_enrich.result()
        hn = f_hn.result()

    top = rank(repos, intent.ranking, now)[:TOP_N]
    md, rows = render(label, intent, top, hn, now)
    message_id = chat.send_message(md, reply_to=user_message_id)[0]
    stamp = now.isoformat()
    for n, title, url, line in rows:
        con.execute(
            "insert or replace into search_result (message_id, n, title, url, line_md, query, created_at) "
            "values (?,?,?,?,?,?,?)",
            (message_id, n, title, url, line, label, stamp),
        )
    return "results"
