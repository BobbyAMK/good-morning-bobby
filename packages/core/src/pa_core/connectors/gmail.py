"""Gmail reads.

Quota arithmetic (verified against Google's published unit costs), so you can see the
headroom rather than guess at it:
    messages.list              =  5 units
    messages.get(metadata)     = 20 units
    messages.get(full)         = 20 units (5 for minimal)
    history.list               =  2 units
4 lists + ~60 metadata gets = 1,220 units, against a ceiling of 6,000 units per user
per MINUTE. The brief is a rounding error.

Strategy: fetch METADATA for everything, bodies only for the handful that survive the
deterministic rule layer. The headers the rules run on (List-Id, Precedence,
Auto-Submitted, Message-ID, In-Reply-To, References) come free with metadata.
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime

import structlog

from ._retry import retry_google
from .base import MailMessage, MailQuery

log = structlog.get_logger("pa.gmail")

METADATA_HEADERS = [
    "From",
    "To",
    "Cc",
    "Subject",
    "Date",
    # these five are what mailrules.py runs on, and they are free with metadata
    "List-Id",
    "List-Unsubscribe",
    "Precedence",
    "Auto-Submitted",
    "Message-ID",
    "In-Reply-To",
    "References",
]

# Category exclusions do most of the filtering work before a token is ever spent.
_NO_BULK = "-category:promotions -category:social -category:updates -category:forums"

QUERIES: dict[MailQuery, str] = {
    MailQuery.OVERNIGHT: f"in:inbox newer_than:16h -from:me {_NO_BULK}",
    MailQuery.ON_THE_HOOK: f"in:inbox is:unread older_than:1d newer_than:10d -from:me {_NO_BULK}",
    MailQuery.IMPORTANT: "in:inbox (is:starred OR is:important) newer_than:14d -from:me",
    MailQuery.MY_OPEN_LOOPS: "in:sent newer_than:21d older_than:3d",
}

_HTML_TAG = re.compile(r"<[^>]+>")
_QUOTE_LINE = re.compile(r"^\s*>", re.M)
# Where a reply's quoted tail begins. Cutting here keeps bodies small and on-topic.
_QUOTE_MARKERS = [
    re.compile(r"^On .{5,80}\bwrote:\s*$", re.M),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.M | re.I),
    re.compile(r"^_{10,}\s*$", re.M),
    re.compile(r"^From:.*$", re.M),
    re.compile(r"^Vào .{5,80}\bđã viết:\s*$", re.M),  # Vietnamese Gmail
]


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode() + b"=" * (-len(data) % 4))


def _walk_parts(payload: dict):
    yield payload
    for p in payload.get("parts", []) or []:
        yield from _walk_parts(p)


def extract_body_text(payload: dict, *, max_chars: int = 4000) -> str:
    """Prefer text/plain; fall back to de-tagged text/html. Strip the quoted tail."""
    plain, html_body = "", ""
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        try:
            text = _b64(data).decode("utf-8", errors="replace")
        except Exception:
            continue
        if mime == "text/plain" and not plain:
            plain = text
        elif mime == "text/html" and not html_body:
            html_body = text

    body = plain or _HTML_TAG.sub(" ", html_body)
    cut = len(body)
    for rx in _QUOTE_MARKERS:
        m = rx.search(body)
        if m and m.start() < cut:
            cut = m.start()
    body = body[:cut]
    body = _QUOTE_LINE.sub("", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:max_chars]


def _received(headers: dict[str, str], internal_ms: str | None) -> datetime:
    if internal_ms:
        return datetime.fromtimestamp(int(internal_ms) / 1000, tz=UTC)
    raw = headers.get("date")
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(UTC)
        except TypeError, ValueError:
            pass
    return datetime.now(UTC)


def _addrs(value: str) -> tuple[str, ...]:
    return tuple(a.lower() for a in (parseaddr(p)[1] for p in value.split(",")) if a)


class Gmail:
    name = "gmail"

    def __init__(self, service=None, *, scopes: list[str] | None = None) -> None:
        if service is None:
            from .google_auth import build_service

            service = build_service("gmail", "v1", scopes=scopes)
        self._s = service
        self._label_cache: dict[str, str] | None = None

    # ------------------------------------------------------------------ reads
    def profile_email(self) -> str:
        return retry_google(lambda: self._s.users().getProfile(userId="me").execute()).get(
            "emailAddress", ""
        )

    def list_labels(self) -> list[tuple[str, str]]:
        res = retry_google(lambda: self._s.users().labels().list(userId="me").execute())
        return [(x["id"], x["name"]) for x in res.get("labels", [])]

    def label_ids_by_name(self) -> dict[str, str]:
        """Store label IDs, never names: renaming a label in the Gmail UI changes the
        name but not the id, and a name-keyed lookup silently stops matching."""
        if self._label_cache is None:
            self._label_cache = {name: lid for lid, name in self.list_labels()}
        return self._label_cache

    def list_messages(
        self,
        *,
        query: MailQuery | str,
        limit: int = 50,
        include_body: bool = False,
    ) -> list[MailMessage]:
        q = QUERIES[query] if isinstance(query, MailQuery) else query
        ids: list[str] = []
        page = None
        while len(ids) < limit:
            # Bind BOTH loop-dependent values as defaults. `ids` grows each iteration,
            # so capturing it by reference would make a retry recompute maxResults from
            # mutated state -- harmless today, a confusing bug the day it matters.
            want = min(100, limit - len(ids))
            res = retry_google(
                lambda p=page, n=want: (
                    self._s.users()
                    .messages()
                    .list(userId="me", q=q, maxResults=n, pageToken=p)
                    .execute()
                )
            )  # 5 units
            ids += [m["id"] for m in res.get("messages", [])]
            page = res.get("nextPageToken")
            if not page:
                break
        return [self.get_message(i, include_body=include_body) for i in ids[:limit]]

    def get_message(self, message_id: str, *, include_body: bool = False) -> MailMessage:
        fmt = "full" if include_body else "metadata"
        kwargs = {"userId": "me", "id": message_id, "format": fmt}
        if not include_body:
            kwargs["metadataHeaders"] = METADATA_HEADERS
        msg = retry_google(lambda: self._s.users().messages().get(**kwargs).execute())  # 20 units

        payload = msg.get("payload", {}) or {}
        headers = {h["name"].lower(): h["value"] for h in (payload.get("headers", []) or [])}
        name, addr = parseaddr(headers.get("from", ""))
        labels = tuple(msg.get("labelIds", []) or [])
        return MailMessage(
            id=msg["id"],
            thread_id=msg.get("threadId", ""),
            from_addr=addr.lower(),
            from_name=name,
            to=_addrs(headers.get("to", "")),
            cc=_addrs(headers.get("cc", "")),
            subject=headers.get("subject", ""),
            # NOTE: whether `snippet` is returned under format='metadata' is the one
            # thing Google's docs do not state. If it is, project 1 never needs bodies.
            snippet=msg.get("snippet", "") or "",
            received_at=_received(headers, msg.get("internalDate")),
            is_unread="UNREAD" in labels,
            labels=labels,
            headers=headers,
            body_text=extract_body_text(payload) if include_body else "",
        )

    def thread_last_sender_is_me(self, thread_id: str, me: str) -> bool:
        """Deterministic 'I spoke last and nobody answered' signal -- no LLM needed."""
        res = retry_google(
            lambda: (
                self._s.users()
                .threads()
                .get(userId="me", id=thread_id, format="metadata", metadataHeaders=["From", "Date"])
                .execute()
            )
        )  # 40 units -- call sparingly
        msgs = res.get("messages", []) or []
        if not msgs:
            return False
        hdrs = {
            h["name"].lower(): h["value"]
            for h in (msgs[-1].get("payload", {}).get("headers", []) or [])
        }
        return parseaddr(hdrs.get("from", ""))[1].lower() == me.lower()

    def history_since(self, start_history_id: str) -> tuple[list[str], str | None]:
        """Incremental new-message ids. Only 2 quota units.

        Raises HttpError 404 when start_history_id has expired (app idle ~7+ days) --
        the caller MUST have a full-resync fallback. It will fire.
        """
        added: list[str] = []
        page, newest = None, None
        while True:
            res = retry_google(
                lambda p=page: (
                    self._s.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded"],
                        pageToken=p,
                    )
                    .execute()
                )
            )
            for h in res.get("history", []) or []:
                newest = h.get("id", newest)
                for ma in h.get("messagesAdded", []) or []:
                    added.append(ma["message"]["id"])
            page = res.get("nextPageToken")
            if not page:
                return added, res.get("historyId") or newest

    def current_history_id(self) -> str:
        return retry_google(lambda: self._s.users().getProfile(userId="me").execute())["historyId"]
