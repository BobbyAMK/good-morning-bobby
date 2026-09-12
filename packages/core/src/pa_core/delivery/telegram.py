"""Telegram sink -- HTML parse mode, NOT MarkdownV2.

This is the opinionated call that defuses a real footgun. MarkdownV2 requires
backslash-escaping 18 characters -- _ * [ ] ( ) ~ ` > # + - = | { } . ! -- INCLUDING
the period, with different rules inside pre/code and inside link URLs. LLM-generated
prose is guaranteed to contain periods, hyphens and parentheses, so every brief is a
potential `400 Bad Request: can't parse entities`, which for a 6am job means silent
non-delivery.

HTML mode needs only & < > escaped, and supports everything we actually use.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Iterable

import httpx2 as httpx

from .base import Delivered

API = "https://api.telegram.org"

TG_LIMIT = 4096  # documented sendMessage text limit
SAFE_MARGIN = 128  # headroom, because the limit counts post-parse entities

# The full MarkdownV2 special set, kept for reference and for escape_markdown_v2().
# We do NOT use MarkdownV2 for agent output.
_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_MDV2_RE = re.compile("([" + re.escape(_MDV2_SPECIALS) + "])")

_ALLOWED_TAGS = ("b", "i", "u", "s", "code", "pre", "a", "blockquote")
_TAG_RE = re.compile(r"<(/?)(" + "|".join(_ALLOWED_TAGS) + r")\b[^>]*>")


def escape_markdown_v2(s: str) -> str:
    """Provided for completeness; unused by the agents. See the module docstring."""
    return _MDV2_RE.sub(r"\\\1", s)


def markdown_to_telegram_html(md: str) -> str:
    """Convert the small Markdown subset our agents emit into Telegram HTML.

    Order matters: escape FIRST, then introduce tags, so we never escape our own tags.
    """
    out: list[str] = []
    in_fence = False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            out.append("<pre>" if not in_fence else "</pre>")
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(html.escape(line, quote=False))
            continue

        s = html.escape(line, quote=False)  # & < >  -> entities
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)  # inline code before emphasis
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", s)
        s = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"^\s*(#{1,6})\s*(.+)$", r"<b>\2</b>", s)  # Telegram has no headings
        s = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", s)  # real bullet char
        s = re.sub(r"^\s*---+\s*$", "———", s)  # hr -> em dashes
        out.append(s)
    if in_fence:
        out.append("</pre>")  # never ship an unclosed tag
    return "\n".join(out)


def chunk_html(s: str, limit: int = TG_LIMIT - SAFE_MARGIN) -> list[str]:
    """Split into pieces that each parse as standalone valid HTML.

    Two rules: never split inside a tag or an entity, and re-open any tag that was
    open at the split point so the next chunk is valid on its own.
    """
    if len(s) <= limit:
        return [s] if s.strip() else []

    chunks: list[str] = []
    buf = ""
    open_tags: list[str] = []

    def track(fragment: str) -> None:
        for closing, tag in _TAG_RE.findall(fragment):
            if closing:
                if tag in open_tags:
                    open_tags.reverse()
                    open_tags.remove(tag)
                    open_tags.reverse()
            else:
                open_tags.append(tag)

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            closing = "".join(f"</{t}>" for t in reversed(open_tags))
            chunks.append(buf + closing)
        buf = "".join(f"<{t}>" for t in open_tags)

    for block in s.split("\n\n"):
        piece = block + "\n\n"
        if len(buf) + len(piece) > limit:
            flush()
            while len(piece) > limit:
                cut = _safe_cut(piece, limit)
                head, piece = piece[:cut], piece[cut:]
                track(head)
                buf += head
                flush()
        track(piece)
        buf += piece
    flush()
    return [c for c in chunks if c.strip()]


def _safe_cut(s: str, limit: int) -> int:
    """Find a cut index <= limit that is not inside a tag or an HTML entity."""
    cut = limit
    # back off out of a partial tag
    lt, gt = s.rfind("<", 0, cut), s.rfind(">", 0, cut)
    if lt > gt:
        cut = lt
    # back off out of a partial entity
    amp, semi = s.rfind("&", 0, cut), s.rfind(";", 0, cut)
    if amp > semi and cut - amp < 12:
        cut = amp
    for sep in ("\n", " "):
        idx = s.rfind(sep, 0, cut)
        if idx > limit // 2:
            return idx + 1
    return max(cut, 1)


class TelegramSink:
    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        client: httpx.Client | None = None,
        max_retries: int = 3,
    ) -> None:
        self._token = token
        self._chat = chat_id
        self._retries = max_retries
        self._c = client or httpx.Client(base_url=API, timeout=httpx.Timeout(30.0, connect=5.0))

    def _url(self, method: str) -> str:
        return f"/bot{self._token}/{method}"

    def _post(self, method: str, payload: dict) -> dict:
        """One call with 429-aware retry. Telegram returns parameters.retry_after."""
        last = ""
        for attempt in range(1, self._retries + 1):
            r = self._c.post(self._url(method), json=payload)
            if r.status_code == 200:
                return r.json()
            try:
                body = r.json()
            except Exception:
                body = {"description": r.text[:200]}
            last = str(body.get("description", ""))
            if r.status_code == 429:
                wait = float(body.get("parameters", {}).get("retry_after", 2))
                time.sleep(min(wait, 30) + 0.25)
                continue
            if r.status_code >= 500 and attempt < self._retries:
                time.sleep(2**attempt)
                continue
            raise _TgError(r.status_code, last)
        raise _TgError(429, last or "rate limited")

    def deliver(
        self, *, title: str, body_markdown: str, run_id: str, urgent: bool = False
    ) -> Delivered:
        body = f"# {title}\n\n{body_markdown}" if title else body_markdown
        first_id: str | None = None
        try:
            for part in chunk_html(markdown_to_telegram_html(body)):
                res = self._send_one(part, urgent=urgent)
                first_id = first_id or str(res.get("result", {}).get("message_id", ""))
                time.sleep(0.35)  # 1 msg/s per chat; stay under it
            return Delivered(self.name, first_id, True)
        except Exception as e:
            return Delivered(self.name, None, False, f"{type(e).__name__}: {e}"[:300])

    def _send_one(self, text: str, *, urgent: bool) -> dict:
        payload = {
            "chat_id": self._chat,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "disable_notification": not urgent,
        }
        try:
            return self._post("sendMessage", payload)
        except _TgError as e:
            if e.status == 400 and "entit" in e.description.lower():
                # Last-resort fallback: strip all markup and send plain text, so a
                # parse error degrades the formatting instead of losing the message.
                plain = re.sub(r"<[^>]+>", "", text)
                plain = html.unescape(plain)
                return self._post(
                    "sendMessage",
                    {
                        "chat_id": self._chat,
                        "text": plain[:TG_LIMIT],
                        "disable_notification": not urgent,
                    },
                )
            raise

    def send_document(self, path: str, caption: str = "") -> dict:
        with open(path, "rb") as fh:
            r = self._c.post(
                self._url("sendDocument"),
                data={"chat_id": self._chat, "caption": caption[:1024]},
                files={"document": (path.rsplit("/", 1)[-1], fh, "text/markdown")},
            )
        r.raise_for_status()
        return r.json()

    def get_updates(self, *, offset: int | None = None, timeout: int = 0) -> list[dict]:
        """getUpdates and setWebhook are mutually exclusive; a local Mac has no public
        URL, so long-polling getUpdates is the only option for inbound."""
        payload: dict = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        res = self._post("getUpdates", payload)
        return res.get("result", [])

    def me(self) -> dict:
        return self._post("getMe", {}).get("result", {})


class _TgError(Exception):
    def __init__(self, status: int, description: str) -> None:
        super().__init__(f"telegram {status}: {description}")
        self.status = status
        self.description = description


def discover_chat_id(token: str, client: httpx.Client | None = None) -> str | None:
    """Read the chat id of the most recent message sent TO the bot.

    Used once by `telegram-bind`, which pins it to an allow-list. Without pinning,
    anyone who finds your bot can talk to your agents.
    """
    sink = TelegramSink(token, chat_id="0", client=client)
    for upd in reversed(sink.get_updates()):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            return str(chat["id"])
    return None


def iter_allowed(updates: Iterable[dict], allowed_chat_id: str) -> Iterable[dict]:
    """Inbound allow-list. Drop anything not from the bound chat."""
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message") or {}
        if str((msg.get("chat") or {}).get("id")) == str(allowed_chat_id):
            yield upd
