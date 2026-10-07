"""Live eval for /search: real model, real GitHub/npm/PyPI. Run by hand: $0, ~1-2 minutes.

    .venv/bin/python evals/run_search.py

A case passes when enough of the expected projects appear in the top 5. The thresholds
are the definition of "search works" -- change ranking or the model, re-run, compare.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime

import structlog
from pa_core.config import load
from pa_core.harness.run import build_llm
from pa_core.llm.usage import UsageLog
from pa_core.store import db, migrate
from pa_morning_brief import search, sources

# (query, expected repo names -- matched against the repo part of owner/repo, need, top_k)
CASES: list[tuple[str, list[str], int]] = [
    (
        "tìm cho tôi các ReactJS Animation library đang hot nhất hiện nay",
        ["motion", "react-spring", "auto-animate"],
        2,
    ),
    ("thư viện HTTP client cho Python", ["httpx", "requests", "aiohttp"], 2),
    ("state management cho React", ["zustand", "redux", "jotai"], 2),
    ("công cụ lint Python nhanh", ["ruff"], 1),
    ("thư viện vẽ biểu đồ cho React", ["recharts", "react-chartjs-2", "visx"], 2),
]
DECLINE = ["giá vàng hôm nay bao nhiêu", "thời tiết Hà Nội ngày mai"]


class Capture:
    def __init__(self) -> None:
        self.text = ""

    def send_message(self, md, *, reply_to=None, notify=False):
        self.text = md
        return [1]

    def pin(self, _):
        pass


def main() -> int:
    cfg = load()
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    migrate.migrate(con, db.MIGRATIONS)
    llm = build_llm(cfg, UsageLog(con, None))
    http = sources.client()
    log = structlog.get_logger("eval")
    failures = 0

    for query, expected, need in CASES:
        intent, how = search.understand(llm, query, log)
        repos = search.rank(
            search.search_github(http, intent, datetime.now(UTC), None),
            intent.ranking,
            datetime.now(UTC),
        )
        if intent.ecosystem == "javascript":
            search.enrich_npm(http, repos, intent.keywords)
        elif intent.ecosystem == "python":
            search.enrich_pypi(http, repos)
        top5 = [
            r.full_name.split("/", 1)[1].lower()
            for r in search.rank(repos, intent.ranking, datetime.now(UTC))[:5]
        ]
        hits = [e for e in expected if e in top5]
        ok = len(hits) >= need
        failures += not ok
        print(
            f"{'ok   ' if ok else 'FAIL '} {len(hits)}/{need} {query[:50]:50s} kw={intent.keywords} ({how}) top5={top5}"
        )

    for query in DECLINE:
        chat = Capture()
        out = search.run(
            chat,
            con,
            llm=llm,
            http=http,
            query=query,
            user_message_id=1,
            now=datetime.now(UTC),
            log=log,
        )
        ok = out == "not_software"
        failures += not ok
        print(f"{'ok   ' if ok else 'FAIL '} decline {query!r} -> {out}")

    print(f"\n{len(CASES) + len(DECLINE) - failures}/{len(CASES) + len(DECLINE)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
