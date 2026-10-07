"""Save items by replying with their numbers. One save = one note message, pinned.

The reply is resolved in code, never by a model: the replied-to message id is looked up
among /search results first, then among delivered briefs, and the numbers index into the
list that message showed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol

from pa_core.store.db import tx

from .items import canonical_url
from .tech_render import _safe

MAX_NUMBERS = 30


class Chat(Protocol):
    def send_message(
        self, body_markdown: str, *, reply_to: int | None = None, notify: bool = False
    ) -> list[int]: ...
    def pin(self, message_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class Entry:
    n: int
    title: str
    url: str
    line_md: str  # what the note shows for this item
    origin: str  # e.g. "Tech Brief · Mon 14 Sep" or "/search react animation"


def parse_numbers(text: str) -> list[int] | None:
    """'3 7', '3,7', '#3', '1-3', '2 - 4' (en/em dashes too) -> sorted unique numbers.

    None when the text is not purely numbers and separators: "save the uv one" is not a
    save command, and guessing would pin the wrong thing.
    """
    t = text.strip()
    if not t or not re.fullmatch(r"[#\d\s,;.\-\u2013\u2014]+", t) or not re.search(r"\d", t):
        return None
    nums: set[int] = set()
    for a, b, single in re.findall(r"(\d+)\s*[-\u2013\u2014]\s*(\d+)|(\d+)", t):
        if single:
            nums.add(int(single))
        else:
            lo, hi = sorted((int(a), int(b)))
            nums.update(range(lo, min(hi, lo + MAX_NUMBERS) + 1))
    return sorted(n for n in nums if n > 0)[:MAX_NUMBERS] or None


def lookup(con: sqlite3.Connection, message_id: int) -> dict[int, Entry] | None:
    """The numbered list shown by `message_id`, or None if it is not one of ours."""
    rows = con.execute(
        "select n, title, url, line_md, query from search_result where message_id = ? order by n",
        (message_id,),
    ).fetchall()
    if rows:
        return {
            r["n"]: Entry(r["n"], r["title"], r["url"], r["line_md"], f"/search {r['query']}")
            for r in rows
        }

    run = con.execute(
        """select a.logical_date from delivery d join agent_run a using (run_id)
            where d.sink = 'telegram' and d.ok = 1 and a.dry_run = 0
              and (',' || d.external_id || ',') like ('%,' || ? || ',%')
            order by d.delivered_at desc limit 1""",
        (str(message_id),),
    ).fetchone()
    if run is None:
        return None
    day = datetime.fromisoformat(run["logical_date"])
    origin = f"Tech Brief · {day:%a} {day.day} {day:%b}"
    items = con.execute(
        "select n, title, url, summary from sent_item where sent_on = ? and n is not null order by n",
        (run["logical_date"],),
    ).fetchall()
    return {
        r["n"]: Entry(
            r["n"],
            r["title"],
            r["url"],
            f"[{_safe(r['title'])}]({r['url']})"
            + (f" - {_safe(r['summary'])}" if r["summary"] else ""),
            origin,
        )
        for r in items
    } or None


def _span(ns: list[int]) -> str:
    return f"1-{max(ns)}" if ns else "none"


def save(
    chat: Chat,
    con: sqlite3.Connection,
    *,
    reply_to_id: int,
    user_message_id: int,
    numbers: list[int],
    now: datetime,
) -> str:
    """Returns a short outcome label (for logs and tests)."""
    entries = lookup(con, reply_to_id)
    if entries is None:
        chat.send_message(
            "⚠ Mình chỉ lưu được tin từ *Tech Brief* hoặc kết quả */search*. "
            "Hãy reply vào đúng tin đó bằng số thứ tự, ví dụ `3 7`.",
            reply_to=user_message_id,
        )
        return "not_a_list"

    missing = [n for n in numbers if n not in entries]
    wanted = [entries[n] for n in numbers if n in entries]
    if not wanted:
        chat.send_message(
            f"⚠ Tin này có các số {_span(list(entries))}, không có số {', '.join(map(str, missing))}.",
            reply_to=user_message_id,
        )
        return "out_of_range"

    fresh: list[Entry] = []
    already: dict[int, list[int]] = {}  # existing note id -> item numbers
    for e in wanted:
        row = con.execute(
            "select note_message_id from saved_item where url_key = ?", (canonical_url(e.url),)
        ).fetchone()
        if row:
            already.setdefault(row["note_message_id"], []).append(e.n)
        else:
            fresh.append(e)

    for note_id, ns in already.items():
        chat.send_message(
            f"↑ #{', #'.join(map(str, ns))} đã được lưu trong note này.", reply_to=note_id
        )

    if missing:
        chat.send_message(
            f"⚠ Bỏ qua số {', '.join(map(str, missing))} - tin này chỉ có {_span(list(entries))}.",
            reply_to=user_message_id,
        )

    if not fresh:
        return "already_saved"

    lines = [f"📌 **Saved** · {now:%a} {now.day} {now:%b %Y}", ""]
    for e in fresh:
        lines.append(f"- {e.line_md}")
    lines.append(f"\n_{fresh[0].origin} · #{', #'.join(str(e.n) for e in fresh)}_")
    note_id = chat.send_message("\n".join(lines))[0]

    outcome = "saved"
    try:
        chat.pin(note_id)
    except Exception as e:  # the note exists either way; say why it is not pinned
        chat.send_message(f"⚠ Đã lưu nhưng không pin được: {e}", reply_to=note_id)
        outcome = "saved_unpinned"

    stamp = now.isoformat()
    with tx(con):
        con.execute(
            "insert into saved_note (note_message_id, source_message_id, created_at, items_json) "
            "values (?,?,?,?)",
            (
                note_id,
                reply_to_id,
                stamp,
                json.dumps([asdict(e) for e in fresh], ensure_ascii=False),
            ),
        )
        for e in fresh:
            con.execute(
                "insert or replace into saved_item (url_key, note_message_id, saved_at) values (?,?,?)",
                (canonical_url(e.url), note_id, stamp),
            )
    return outcome


def recent_notes_md(con: sqlite3.Connection, limit: int = 10) -> str:
    rows = con.execute(
        "select created_at, items_json from saved_note order by created_at desc limit ?", (limit,)
    ).fetchall()
    if not rows:
        return "Chưa có note nào. Reply số thứ tự vào Tech Brief hoặc kết quả /search để lưu."
    out = [f"📌 **{len(rows)} lần lưu gần nhất** - xem đầy đủ trong danh sách tin đã pin", ""]
    for r in rows:
        day = datetime.fromisoformat(r["created_at"])
        for e in json.loads(r["items_json"]):
            out.append(f"- {day:%d/%m} · [{_safe(e['title'])}]({e['url']})")
    return "\n".join(out)
