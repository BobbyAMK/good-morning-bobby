"""pa-bot: the inbound side of the Telegram chat. launchd KeepAlive; long-polls getUpdates.

  reply "3 7" to a brief or a /search result  -> one note with those items, pinned
  /search <what you need>                     -> numbered top libraries/repos/tools
  /notes                                      -> recent saves
  /help, /start                               -> usage

Only the bound chat id is served (the same allow-list the brief delivers to). Everything
else is logged and dropped. The update offset is stored AFTER an update is handled, so a
crash re-handles at most one message instead of silently losing it. While the Mac sleeps,
Telegram holds updates for up to 24 hours.
"""

from __future__ import annotations

import contextlib
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from pa_core import logging as pa_logging
from pa_core import secrets as sec
from pa_core.config import Settings, load
from pa_core.delivery.telegram import TelegramError, TelegramSink
from pa_core.harness.run import build_llm
from pa_core.llm.usage import UsageLog
from pa_core.store import db, migrate

from . import notes, search, sources

log = structlog.get_logger("pa.bot")

OFFSET_KEY = "telegram:update_offset"
POLL_TIMEOUT = 50
COMMANDS = [
    ("search", "Tìm thư viện, repo, tool - ví dụ: /search react animation"),
    ("notes", "Các mẩu tin đã lưu gần đây"),
    ("help", "Cách dùng bot"),
]
HELP = """\
**Tech Brief bot**

📌 **Lưu tin:** reply vào Tech Brief hoặc kết quả /search bằng số thứ tự, ví dụ `3 7` hoặc `2-4`. \
Bot gửi lại một note chứa các tin đó và pin nó.

🔎 **/search** `<điều bạn cần>` - tìm thư viện, framework, tool, repo trên GitHub, npm, PyPI, \
Hacker News. Ví dụ: `/search ReactJS animation library đang hot nhất hiện nay`

🗂 **/notes** - các lần lưu gần đây."""


@dataclass
class Bot:
    cfg: Settings
    con: sqlite3.Connection
    chat: Any  # TelegramSink, or a fake in tests
    chat_id: str
    llm: Any
    http: Any
    github_token: str | None = None

    def handle(self, update: dict, now: datetime | None = None) -> str:
        """One update -> an outcome label. Never raises for a user-caused problem."""
        now = now or datetime.now(UTC)
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
            log.warning("bot.foreign_chat", chat=(msg.get("chat") or {}).get("id"))
            return "ignored_chat"
        text = (msg.get("text") or "").strip()
        mid = int(msg.get("message_id") or 0)
        reply = msg.get("reply_to_message")

        if text.startswith("/"):
            head, _, arg = text.partition(" ")
            cmd = head.split("@", 1)[0].lower()
            if cmd == "/search":
                self._typing()
                return search.run(
                    self.chat,
                    self.con,
                    llm=self.llm,
                    http=self.http,
                    query=arg,
                    user_message_id=mid,
                    now=now,
                    log=log,
                    github_token=self.github_token,
                )
            if cmd == "/notes":
                self.chat.send_message(notes.recent_notes_md(self.con), reply_to=mid)
                return "notes"
            self.chat.send_message(HELP, reply_to=mid)
            return "help"

        if reply:
            numbers = notes.parse_numbers(text)
            if numbers:
                return notes.save(
                    self.chat,
                    self.con,
                    reply_to_id=int(reply["message_id"]),
                    user_message_id=mid,
                    numbers=numbers,
                    now=now.astimezone(self.cfg.zone),
                )
            self.chat.send_message(
                "Để lưu tin, reply bằng **số thứ tự**, ví dụ `3 7` hoặc `2-4`.", reply_to=mid
            )
            return "reply_without_numbers"

        self.chat.send_message(HELP, reply_to=mid)
        return "help"

    def _typing(self) -> None:
        with contextlib.suppress(Exception):  # cosmetic
            self.chat.typing()


def poll_once(bot: Bot, offset: int | None) -> int | None:
    """Fetch and handle one batch. Returns the next offset."""
    updates = bot.chat.get_updates(offset=offset, timeout=POLL_TIMEOUT, allowed_updates=["message"])
    for upd in updates:
        try:
            outcome = bot.handle(upd)
            log.info("bot.handled", update_id=upd.get("update_id"), outcome=outcome)
        except Exception as e:
            log.error(
                "bot.handler_failed",
                update_id=upd.get("update_id"),
                error=f"{type(e).__name__}: {e}"[:300],
            )
            try:
                mid = (upd.get("message") or {}).get("message_id")
                bot.chat.send_message(
                    f"⚠ Lỗi khi xử lý: `{type(e).__name__}`. Chi tiết trong bot.err.log.",
                    reply_to=mid,
                )
            except Exception:
                pass
        offset = int(upd["update_id"]) + 1
        db.kv_put(
            bot.con, OFFSET_KEY, str(offset)
        )  # after handling: a crash re-handles, never drops
    return offset


def main() -> None:
    pa_logging.configure(level="INFO")
    cfg = load()
    cfg.ensure_dirs()
    con = db.connect(cfg.platform_db)
    migrate.migrate(con, db.MIGRATIONS)

    token = sec.get_optional("telegram_token")
    chat_id = db.kv_get(con, "telegram:chat_id")
    if not token or not chat_id:
        log.error("bot.not_configured", hint="just telegram-token; just telegram-bind")
        time.sleep(300)  # don't let KeepAlive spin; launchd restarts us afterwards
        raise SystemExit(1)

    chat = TelegramSink(token, chat_id)
    bot = Bot(
        cfg=cfg,
        con=con,
        chat=chat,
        chat_id=chat_id,
        llm=build_llm(cfg, UsageLog(con, cfg.monthly_budget_usd)),
        http=sources.client(timeout=20.0),
        github_token=sec.get_optional("github_token"),
    )

    stopping = False

    def stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True
        log.info("bot.stopping")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        chat.set_my_commands(COMMANDS)
    except Exception as e:
        log.warning("bot.set_commands_failed", error=str(e)[:200])

    raw = db.kv_get(con, OFFSET_KEY)
    offset = int(raw) if raw else None
    if offset is None:
        # First start ever: acknowledge the backlog instead of answering it. Otherwise the
        # "hi" sent during telegram-bind, hours ago, gets a help message out of nowhere.
        try:
            last = chat.get_updates(offset=-1, timeout=0)
            if last:
                offset = int(last[-1]["update_id"]) + 1
                db.kv_put(con, OFFSET_KEY, str(offset))
                log.info("bot.backlog_skipped", next_offset=offset)
        except Exception as e:
            log.warning("bot.backlog_skip_failed", error=str(e)[:200])
    log.info("bot.started", chat=chat_id, offset=offset)
    backoff = 5.0
    while not stopping:
        try:
            offset = poll_once(bot, offset)
            backoff = 5.0
            db.kv_put(con, "heartbeat:bot", datetime.now(UTC).isoformat())
        except TelegramError as e:
            if e.status == 409:  # another getUpdates consumer, e.g. telegram-bind
                log.warning("bot.conflict", error=e.description[:200])
                time.sleep(30)
            elif e.status == 401:
                log.error("bot.unauthorized", hint="token revoked? just telegram-token")
                time.sleep(300)
            else:
                log.warning("bot.telegram_error", status=e.status, error=e.description[:200])
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        except Exception as e:  # network down, Mac just woke, DNS not ready
            log.warning("bot.poll_failed", error=f"{type(e).__name__}: {e}"[:200])
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    log.info("bot.stopped")


if __name__ == "__main__":
    main()
