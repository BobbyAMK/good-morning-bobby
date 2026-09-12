"""Provider-agnostic mail and calendar interfaces.

Three rules keep this honest, and breaking rule 3 is how people accidentally
hard-code Gmail forever:
  1. No Google type ever crosses this boundary.
  2. Every datetime is tz-aware UTC here; local conversion happens in the renderer.
  3. Agents ask for a NAMED query (MailQuery.*), never a raw Gmail search string.
     The translation to Gmail syntax lives inside gmail.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable


class MailQuery(StrEnum):
    """Named intents. gmail.py owns the translation to Gmail search operators."""

    OVERNIGHT = "overnight"  # arrived since last night, not bulk
    ON_THE_HOOK = "on_the_hook"  # unread, aging, addressed to me
    IMPORTANT = "important"  # starred or Gmail-flagged important
    MY_OPEN_LOOPS = "my_open_loops"  # I spoke last and nobody answered


@dataclass(frozen=True, slots=True)
class MailMessage:
    id: str
    thread_id: str
    from_addr: str
    from_name: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    snippet: str
    received_at: datetime  # tz-aware UTC
    is_unread: bool
    labels: tuple[str, ...]
    headers: dict[str, str] = field(default_factory=dict)  # lowercased keys
    body_text: str = ""  # only populated when include_body=True
    provider: str = "gmail"

    @property
    def from_domain(self) -> str:
        return self.from_addr.rsplit("@", 1)[-1].lower() if "@" in self.from_addr else ""

    @property
    def local_part(self) -> str:
        return self.from_addr.split("@", 1)[0].lower()

    @property
    def age(self) -> timedelta:
        return datetime.now(self.received_at.tzinfo) - self.received_at


@dataclass(frozen=True, slots=True)
class DraftSpec:
    to: tuple[str, ...]
    subject: str
    body_text: str
    cc: tuple[str, ...] = ()
    thread_id: str | None = None
    in_reply_to: str | None = None  # the parent's RFC 2822 Message-ID
    references: str | None = None  # the full chain


@runtime_checkable
class MailProvider(Protocol):
    name: str

    # --- reads: all project 1 needs -------------------------------------
    def list_messages(
        self,
        *,
        query: MailQuery,
        limit: int = 50,
        include_body: bool = False,
    ) -> list[MailMessage]: ...

    def get_message(self, message_id: str, *, include_body: bool = True) -> MailMessage: ...

    def list_labels(self) -> list[tuple[str, str]]: ...

    # --- writes: deferred to project 3 ----------------------------------
    def create_draft(self, spec: DraftSpec) -> str: ...

    def add_labels(self, message_id: str, label_ids: list[str]) -> None: ...


@dataclass(frozen=True, slots=True)
class Attendee:
    email: str
    name: str | None
    response: Literal["accepted", "declined", "tentative", "needsAction"]
    optional: bool = False
    organizer: bool = False
    is_self: bool = False
    resource: bool = False  # a meeting room, not a person

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower() if "@" in self.email else ""


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    id: str
    calendar_id: str
    title: str
    start: datetime  # tz-aware UTC
    end: datetime
    all_day: bool
    location: str | None
    description: str | None
    attendees: tuple[Attendee, ...]
    conference_url: str | None
    status: Literal["confirmed", "tentative", "cancelled"]
    recurring_event_id: str | None
    event_type: str = "default"
    transparency: str = "opaque"  # "transparent" == marked Free
    my_response: str | None = None
    provider: str = "gcal"

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def others(self) -> list[Attendee]:
        return [
            a
            for a in self.attendees
            if not a.is_self and not a.resource and a.response != "declined" and "@" in a.email
        ]

    def external_domains(self, internal: set[str]) -> set[str]:
        return {a.domain for a in self.others()} - internal

    def externals(self, internal: set[str]) -> list[Attendee]:
        """External PEOPLE, not domains. Three colleagues from one company is three
        people to prepare for, and "(1 external)" would read as a much smaller meeting
        than it is."""
        return [a for a in self.others() if a.domain and a.domain not in internal]

    def is_external(self, internal: set[str]) -> bool:
        return bool(self.externals(internal))


@runtime_checkable
class CalendarProvider(Protocol):
    name: str

    def list_calendars(self) -> list[tuple[str, str]]: ...

    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_ids: list[str] | None = None,
        include_declined: bool = False,
    ) -> list[CalendarEvent]: ...
