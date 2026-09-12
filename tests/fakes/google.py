"""Fake Gmail and Calendar that satisfy the Protocols structurally.

Because every external system sits behind a Protocol, these are trivial -- which is the
payoff for that boundary. They let the golden test run the REAL pipeline and the REAL
local model with no Google credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pa_core.connectors.base import Attendee, CalendarEvent, MailMessage, MailQuery

ME = "me@mycorp.com"


def _att(email: str, *, me: bool = False, resp: str = "accepted", room: bool = False) -> Attendee:
    return Attendee(email=email, name=None, response=resp, is_self=me, resource=room)


def _msg(
    i,
    frm,
    name,
    subj,
    body,
    *,
    hours_ago=3,
    headers=None,
    labels=("INBOX", "UNREAD"),
    to=(ME,),
    thread=None,
) -> MailMessage:
    return MailMessage(
        id=f"m{i}",
        thread_id=thread or f"t{i}",
        from_addr=frm,
        from_name=name,
        to=tuple(to),
        cc=(),
        subject=subj,
        snippet=body[:120],
        received_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        is_unread="UNREAD" in labels,
        labels=tuple(labels),
        headers=headers or {},
        body_text=body,
    )


class FakeGmail:
    """A realistic 26-message morning: 3 that matter, the rest noise."""

    name = "gmail"

    def __init__(self) -> None:
        real = [
            _msg(
                1,
                "mai@client.com",
                "Mai Tran",
                "still blocked on the Q4 pricing sheet",
                "Hi — I asked last week too. I can't move the proposal forward without "
                "the Q4 pricing sheet. Can you send it today?",
                hours_ago=30,
            ),
            _msg(
                2,
                "lan@acme.com",
                "Lan Nguyen",
                "agenda for our call today",
                "I've proposed three topics for 09:30: data platform scaling, the "
                "reconciliation incident, and pricing. Does that work?",
                hours_ago=14,
            ),
            _msg(
                3,
                "noreply@techcombank.com.vn",
                "Techcombank",
                "Thẻ của bạn sắp hết hạn 20/09",
                "Thẻ tín dụng của bạn sẽ hết hạn ngày 20/09. Vui lòng đăng nhập để gia hạn.",
                hours_ago=20,
                labels=("INBOX", "UNREAD", "CATEGORY_UPDATES"),
            ),
            _msg(
                4,
                "tuan@acme.com",
                "Tuan Le",
                "quick question on the runbook",
                "Do you have the idempotency runbook handy? Lan mentioned it.",
                hours_ago=5,
            ),
        ]
        noise = (
            [
                _msg(
                    100 + i,
                    f"promo{i}@shop.example",
                    "Shop",
                    f"50% off today #{i}",
                    "Limited time offer!",
                    labels=("INBOX", "CATEGORY_PROMOTIONS"),
                )
                for i in range(12)
            ]
            + [
                _msg(
                    200 + i,
                    f"list{i}@groups.example",
                    "List",
                    f"[python-dev] thread {i}",
                    "Re: PEP discussion",
                    headers={"list-id": f"<l{i}.groups.example>"},
                    to=("python-dev@groups.example",),
                )
                for i in range(5)
            ]
            + [
                _msg(
                    300 + i,
                    f"notifications{i}@github.example",
                    "GitHub",
                    f"[repo] PR #{i} merged",
                    "merged by someone",
                )
                for i in range(3)
            ]
            + [
                _msg(
                    400 + i,
                    "billing@vendor.example",
                    "Vendor",
                    f"Your receipt #{1000 + i}",
                    "Thanks for your payment",
                )
                for i in range(2)
            ]
        )
        self._all = real + noise

    def profile_email(self) -> str:
        return ME

    def list_messages(self, *, query, limit=50, include_body=False) -> list[MailMessage]:
        if query == MailQuery.IMPORTANT:
            return self._all[:2][:limit]
        return self._all[:limit]

    def get_message(self, message_id: str, *, include_body: bool = False) -> MailMessage:
        return next(m for m in self._all if m.id == message_id)

    def list_labels(self):
        return [("Label_1", "AI/needs-reply")]


class FakeCalendar:
    name = "gcal"

    def __init__(self, tz) -> None:
        self._tz = tz

    def list_calendars(self):
        return [("primary", "me")]

    def list_events(self, *, start, end, calendar_ids=None, include_declined=False):
        tz = self._tz
        d0 = start.astimezone(tz).date()
        d1 = d0 + timedelta(days=1)

        def ev(title, day, h1, m1, h2, m2, atts, **kw):
            return CalendarEvent(
                id=f"{title}-{day}",
                calendar_id="primary",
                title=title,
                start=datetime(day.year, day.month, day.day, h1, m1, tzinfo=tz).astimezone(UTC),
                end=datetime(day.year, day.month, day.day, h2, m2, tzinfo=tz).astimezone(UTC),
                all_day=False,
                location=None,
                description=None,
                attendees=tuple(atts),
                conference_url=None,
                status="confirmed",
                recurring_event_id=kw.get("rec"),
                event_type=kw.get("etype", "default"),
                transparency="opaque",
                my_response=None,
            )

        me = _att(ME, me=True)
        return [
            ev(
                "Acme Corp — intro call",
                d0,
                9,
                30,
                10,
                30,
                [me, _att("lan@acme.com"), _att("tuan@acme.com")],
            ),
            ev("1:1 Minh", d0, 11, 0, 11, 30, [me, _att("minh@mycorp.com")]),
            ev(
                "Northwind — pricing review",
                d0,
                14,
                0,
                15,
                30,
                [me, _att("a@northwind.io"), _att("b@northwind.io")],
            ),
            ev("Eng standup", d0, 16, 0, 17, 0, [me, _att("t@mycorp.com")], rec="r1"),
            # tomorrow is heavy -- the brief should notice and say "protect tonight"
            ev("Northwind workshop", d1, 9, 0, 12, 0, [me, _att("a@northwind.io")]),
            ev("Board prep", d1, 13, 0, 16, 0, [me, _att("chair@board.example")]),
        ]
