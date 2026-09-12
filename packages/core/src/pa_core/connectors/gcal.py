"""Google Calendar reads.

NOTE on syncToken: it is the "proper" incremental API but it FORBIDS timeMin/timeMax/
orderBy/q, so you would have to sync the whole calendar and window it client-side. The
windowed query below is cheaper to write and free to run: Calendar allows 600 req/min
per user, and polling every 10 minutes is 0.02% of that.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from ._retry import retry_google
from .base import Attendee, CalendarEvent

log = structlog.get_logger("pa.gcal")

_RESPONSES = {"accepted", "declined", "tentative", "needsAction"}


def _parse_dt(node: dict) -> tuple[datetime, bool]:
    """Returns (tz-aware UTC datetime, all_day)."""
    if "dateTime" in node:
        return datetime.fromisoformat(node["dateTime"]).astimezone(UTC), False
    d = datetime.fromisoformat(node["date"])
    return d.replace(tzinfo=UTC), True


def _attendee(a: dict) -> Attendee:
    resp = a.get("responseStatus", "needsAction")
    return Attendee(
        email=(a.get("email") or "").lower(),
        name=a.get("displayName"),
        response=resp if resp in _RESPONSES else "needsAction",  # type: ignore[arg-type]
        optional=bool(a.get("optional")),
        organizer=bool(a.get("organizer")),
        is_self=bool(a.get("self")),
        resource=bool(a.get("resource")),
    )


def _conference_url(ev: dict) -> str | None:
    for ep in (ev.get("conferenceData", {}) or {}).get("entryPoints", []) or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]
    return ev.get("hangoutLink")


class GoogleCalendar:
    name = "gcal"

    def __init__(self, service=None, *, scopes: list[str] | None = None) -> None:
        if service is None:
            from .google_auth import build_service

            service = build_service("calendar", "v3", scopes=scopes)
        self._s = service

    def list_calendars(self) -> list[tuple[str, str]]:
        res = retry_google(lambda: self._s.calendarList().list(maxResults=50).execute())
        return [(c["id"], c.get("summary", c["id"])) for c in res.get("items", [])]

    def list_events(
        self,
        *,
        start: datetime,
        end: datetime,
        calendar_ids: list[str] | None = None,
        include_declined: bool = False,
    ) -> list[CalendarEvent]:
        out: list[CalendarEvent] = []
        for cal_id in calendar_ids or ["primary"]:
            page = None
            while True:
                res = retry_google(
                    lambda c=cal_id, p=page: (
                        self._s.events()
                        .list(
                            calendarId=c,
                            timeMin=start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                            timeMax=end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                            singleEvents=True,  # required for orderBy=startTime
                            orderBy="startTime",
                            maxResults=250,
                            pageToken=p,
                        )
                        .execute()
                    )
                )
                for ev in res.get("items", []):
                    if ev.get("status") == "cancelled":
                        continue
                    s, all_day = _parse_dt(ev["start"])
                    e, _ = _parse_dt(ev["end"])
                    atts = tuple(_attendee(a) for a in ev.get("attendees", []) or [])
                    mine = next((a for a in atts if a.is_self), None)
                    if not include_declined and mine and mine.response == "declined":
                        continue
                    out.append(
                        CalendarEvent(
                            id=ev["id"],
                            calendar_id=cal_id,
                            title=ev.get("summary", "(no title)"),
                            start=s,
                            end=e,
                            all_day=all_day,
                            location=ev.get("location"),
                            description=ev.get("description"),
                            attendees=atts,
                            conference_url=_conference_url(ev),
                            status=ev.get("status", "confirmed"),  # type: ignore[arg-type]
                            recurring_event_id=ev.get("recurringEventId"),
                            event_type=ev.get("eventType", "default"),
                            transparency=ev.get("transparency", "opaque"),
                            my_response=mine.response if mine else None,
                        )
                    )
                page = res.get("nextPageToken")
                if not page:
                    break
        out.sort(key=lambda x: x.start)
        return out
