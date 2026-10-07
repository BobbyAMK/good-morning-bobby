"""Save-by-reply and the bot loop, against a fake chat and an in-memory DB."""

import sqlite3
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from pa_core.config import Settings
from pa_core.store import db, migrate
from pa_morning_brief import bot as B
from pa_morning_brief import notes
from pa_morning_brief.items import Item
from pa_morning_brief.tech_agent import _remember
from pa_morning_brief.tech_render import numbered

NOW = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
CHAT = "6610600527"


class FakeChat:
    def __init__(self, fail_pin: bool = False):
        self.sent: list[dict] = []
        self.pinned: list[int] = []
        self.fail_pin = fail_pin
        self._next = 500

    def send_message(self, body_markdown, *, reply_to=None, notify=False):
        self._next += 1
        self.sent.append({"id": self._next, "text": body_markdown, "reply_to": reply_to})
        return [self._next]

    def pin(self, message_id):
        if self.fail_pin:
            raise RuntimeError("too many pinned messages")
        self.pinned.append(message_id)

    def typing(self):
        pass


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    migrate.migrate(c, db.MIGRATIONS)
    # a delivered brief for 2026-09-14, split into two Telegram messages 101 and 102
    c.execute(
        "insert into agent_run (run_id, agent, logical_date, dry_run, status, started_at) "
        "values ('r1','morning-brief','2026-09-14',0,'ok','2026-09-13T23:05:00Z')"
    )
    c.execute(
        "insert into delivery (run_id, sink, external_id, delivered_at, ok) "
        "values ('r1','telegram','101,102','2026-09-13T23:06:00Z',1)"
    )
    for n in range(1, 16):
        c.execute(
            "insert into sent_item (url_key, title, section, url, sent_on, n, summary) values (?,?,?,?,?,?,?)",
            (
                f"https://x.com/{n}",
                f"Item {n}",
                "ai",
                f"https://x.com/{n}",
                "2026-09-14",
                n,
                f"summary {n}",
            ),
        )
    return c


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("3 7", [3, 7]),
        ("3,7", [3, 7]),
        ("#3", [3]),
        ("1-3", [1, 2, 3]),
        ("2 \u2013 4", [2, 3, 4]),
        ("7 7 3", [3, 7]),
        ("save the uv one", None),
        ("", None),
        ("0", None),
    ],
)
def test_parse_numbers(text, want):
    assert notes.parse_numbers(text) == want


def test_huge_range_is_capped():
    assert len(notes.parse_numbers("1-100000")) == notes.MAX_NUMBERS


# ---------------------------------------------------------------- saving
def save(chat, con, text, reply_to=102):
    return notes.save(
        chat,
        con,
        reply_to_id=reply_to,
        user_message_id=900,
        numbers=notes.parse_numbers(text),
        now=NOW,
    )


def test_reply_to_second_part_of_a_split_brief_saves_one_pinned_note(con):
    chat = FakeChat()
    assert save(chat, con, "3 7") == "saved"
    assert len(chat.sent) == 1 and chat.pinned == [chat.sent[0]["id"]]
    note = chat.sent[0]["text"]
    assert "Item 3" in note and "Item 7" in note and "https://x.com/7" in note
    assert "#3, #7" in note and "Tech Brief" in note
    assert con.execute("select count(*) from saved_item").fetchone()[0] == 2


def test_saving_again_points_at_the_existing_note_instead_of_a_new_pin(con):
    chat = FakeChat()
    save(chat, con, "3")
    first_note = chat.sent[0]["id"]
    assert save(chat, con, "3") == "already_saved"
    assert chat.pinned == [first_note]
    assert chat.sent[-1]["reply_to"] == first_note


def test_out_of_range_saves_nothing(con):
    chat = FakeChat()
    assert save(chat, con, "18") == "out_of_range"
    assert chat.pinned == [] and "1-15" in chat.sent[0]["text"]


def test_mixed_valid_and_invalid_saves_the_valid_and_warns(con):
    chat = FakeChat()
    assert save(chat, con, "3 18") == "saved"
    assert len(chat.pinned) == 1
    assert any("18" in m["text"] and m["reply_to"] == 900 for m in chat.sent)


def test_reply_to_an_unknown_message_is_explained(con):
    chat = FakeChat()
    assert save(chat, con, "1", reply_to=4242) == "not_a_list"
    assert chat.pinned == []


def test_pin_failure_still_keeps_the_note(con):
    chat = FakeChat(fail_pin=True)
    assert save(chat, con, "2") == "saved_unpinned"
    assert con.execute("select count(*) from saved_note").fetchone()[0] == 1
    assert "không pin được" in chat.sent[-1]["text"]


def test_search_results_are_saveable_and_take_precedence(con):
    con.execute(
        "insert into search_result (message_id, n, title, url, line_md, query, created_at) "
        "values (777, 1, 'motiondivision/motion', 'https://github.com/motiondivision/motion', "
        "'[motiondivision/motion](https://github.com/motiondivision/motion) · ★34k', 'react animation', 'x')"
    )
    chat = FakeChat()
    assert save(chat, con, "1", reply_to=777) == "saved"
    assert "/search react animation" in chat.sent[0]["text"] and "★34k" in chat.sent[0]["text"]


# ---------------------------------------------------------------- brief numbering
def test_same_day_rerun_replaces_numbers(con):
    ctx = SimpleNamespace(con=con, logical_date=date(2026, 9, 14))
    items = [
        Item(section="news", source="s", title=f"New {i}", url=f"https://n.com/{i}", published=None)
        for i in range(2)
    ]
    _remember(ctx, numbered({"ai": [], "trending": [], "releases": [], "news": items}))
    rows = con.execute(
        "select n, title from sent_item where sent_on='2026-09-14' order by n"
    ).fetchall()
    assert [(r["n"], r["title"]) for r in rows] == [(1, "New 0"), (2, "New 1")]


def test_numbering_follows_section_order():
    mk = lambda s, t: Item(section=s, source="s", title=t, url=f"https://e.com/{t}", published=None)
    got = numbered(
        {
            "news": [mk("news", "n")],
            "ai": [mk("ai", "a1"), mk("ai", "a2")],
            "trending": [],
            "releases": [],
        }
    )
    assert [(it.title, n) for it, n in got] == [("a1", 1), ("a2", 2), ("n", 3)]


# ---------------------------------------------------------------- bot loop
def make_bot(con, chat):
    return B.Bot(
        cfg=Settings(_env_file=None), con=con, chat=chat, chat_id=CHAT, llm=None, http=None
    )


def upd(uid, text, *, chat=CHAT, reply_to=None):
    m = {"message_id": 900 + uid, "chat": {"id": int(chat)}, "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": uid, "message": m}


def test_foreign_chat_is_ignored(con):
    chat = FakeChat()
    assert make_bot(con, chat).handle(upd(1, "/help", chat="12345")) == "ignored_chat"
    assert chat.sent == []


def test_reply_with_numbers_saves_and_reply_without_numbers_explains(con):
    chat = FakeChat()
    b = make_bot(con, chat)
    assert b.handle(upd(1, "2 5", reply_to=101), now=NOW) == "saved"
    assert b.handle(upd(2, "save this", reply_to=101), now=NOW) == "reply_without_numbers"
    assert len(chat.pinned) == 1


def test_commands(con):
    chat = FakeChat()
    b = make_bot(con, chat)
    assert b.handle(upd(1, "/start")) == "help"
    assert b.handle(upd(2, "/notes@my_bot")) == "notes"
    assert b.handle(upd(3, "/search")) == "usage"


def test_poll_persists_offset_after_handling_even_when_a_handler_crashes(con):
    chat = FakeChat()
    b = make_bot(con, chat)
    batch = [upd(10, "/help"), upd(11, "/notes")]
    chat.get_updates = lambda **kw: batch
    b.handle = lambda u: (
        (_ for _ in ()).throw(RuntimeError("boom")) if u["update_id"] == 10 else "ok"
    )
    assert B.poll_once(b, None) == 12
    assert db.kv_get(con, B.OFFSET_KEY) == "12"
    assert "RuntimeError" in chat.sent[0]["text"]
