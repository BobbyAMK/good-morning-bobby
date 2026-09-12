"""Pure-function tests -- no network, no Ollama, no Google, no mocks.

These cover roughly the bugs you will actually have: markup escaping, tag-aware
chunking, interval arithmetic, and the rule layer. They need zero infrastructure, which
is why they run on every commit.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from pa_core.connectors.base import Attendee, CalendarEvent, MailMessage
from pa_core.delivery.telegram import (
    TG_LIMIT,
    chunk_html,
    escape_markdown_v2,
    markdown_to_telegram_html,
)
from pa_core.llm.base import Usage
from pa_core.llm.usage import price_usd
from pa_core.mailrules import NOREPLY_RE, Bucket, RuleConfig, apply
from pa_morning_brief.calendar_shape import analyse, merge

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
ME = "me@mycorp.com"
RULES = RuleConfig(
    internal_domains=frozenset({"mycorp.com"}),
    vip_domains=frozenset({"client.com"}),
    my_email=ME,
)


def msg(frm=ME and "a@b.com", subject="hi", headers=None, labels=(), to=(ME,)) -> MailMessage:
    return MailMessage(
        id="x",
        thread_id="t",
        from_addr=frm,
        from_name="",
        to=tuple(to),
        cc=(),
        subject=subject,
        snippet="",
        received_at=datetime.now(UTC),
        is_unread=True,
        labels=tuple(labels),
        headers=headers or {},
    )


# ----------------------------------------------------------------- telegram
def _balanced(s: str) -> bool:
    stack: list[str] = []
    for closing, tag in re.findall(r"<(/?)(b|i|u|s|code|pre|a|blockquote)\b[^>]*>", s):
        if closing:
            if not stack or stack[-1] != tag:
                return False
            stack.pop()
        else:
            stack.append(tag)
    return not stack


def test_html_escapes_only_the_three_characters_it_must():
    # The whole reason we use HTML mode: MarkdownV2 would need 18 characters escaped,
    # including the period, and LLM prose always has periods.
    risky = "Load: 4h15m (3 external) - protect tonight. Cost: $0.09."
    assert escape_markdown_v2(risky) != risky
    assert "\\" not in markdown_to_telegram_html(risky)


@pytest.mark.parametrize(
    "src,want",
    [
        ("**bold**", "<b>bold</b>"),
        ("*em*", "<i>em</i>"),
        ("`code()`", "<code>code()</code>"),
        ("# Heading", "<b>Heading</b>"),
        ("- item", "• item"),
        ("a & b", "a &amp; b"),
        ("<tag>", "&lt;tag&gt;"),
        ("[x](https://e.com?a=1&b=2)", '<a href="https://e.com?a=1&amp;b=2">x</a>'),
    ],
)
def test_markdown_subset_converts(src, want):
    assert want in markdown_to_telegram_html(src)


def test_unclosed_fence_never_ships_an_unclosed_tag():
    out = markdown_to_telegram_html("text\n```\nnever closed")
    assert out.count("<pre>") == out.count("</pre>") == 1


def test_chunks_are_individually_valid_html():
    big = "\n\n".join(f"**Section {i}**\n" + "x" * 400 for i in range(30))
    parts = chunk_html(markdown_to_telegram_html(big))
    assert len(parts) > 1
    assert all(len(p) <= TG_LIMIT for p in parts)
    assert all(_balanced(p) for p in parts)
    assert all("<" not in p.split(">")[-1] for p in parts)


def test_never_splits_an_html_entity():
    parts = chunk_html(markdown_to_telegram_html("x & y " * 1200))
    assert all(not re.search(r"&[a-z]{0,8}$", p) for p in parts)


# ----------------------------------------------------------------- pricing
def test_local_models_are_free_and_cloud_models_are_not():
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert price_usd("qwen2.5:7b-instruct-q4_K_M", u) == 0.0
    assert price_usd("claude-opus-5", u) == pytest.approx(30.0)
    assert price_usd("claude-haiku-4-5", u) == pytest.approx(6.0)


# ----------------------------------------------------------------- mail rules
def test_vips_always_reach_the_llm_whatever_else_says():
    spam_from_a_vip = msg(
        frm="newsletter@client.com",
        subject="your receipt",
        headers={"precedence": "bulk", "list-id": "<n>"},
        labels=("CATEGORY_PROMOTIONS",),
    )
    assert apply(spam_from_a_vip, RULES) is None


@pytest.mark.parametrize(
    "m,rule",
    [
        (msg(headers={"precedence": "Bulk"}), "precedence_bulk"),
        (msg(headers={"auto-submitted": "auto-generated"}), "auto_submitted"),
        (msg(headers={"list-id": "<d>"}, to=("o@x.com",)), "mailing_list"),
        (msg(labels=("CATEGORY_PROMOTIONS",)), "gmail_category"),
        (msg(frm="no-reply@svc.com"), "noreply_sender"),
        (msg(frm="no-reply2@svc.com"), "noreply_sender"),
        (msg(subject="Your verification code is 1"), "otp_or_receipt"),
    ],
)
def test_deterministic_ignores(m, rule):
    d = apply(m, RULES)
    assert d is not None and d.rule == rule and d.bucket == Bucket.IGNORE


def test_auto_submitted_no_is_not_automation():
    assert apply(msg(headers={"auto-submitted": "no"}), RULES) is None


def test_category_updates_is_never_auto_ignored():
    # CATEGORY_UPDATES holds both "your flight is delayed" and newsletters.
    assert apply(msg(labels=("CATEGORY_UPDATES",)), RULES) is None


@pytest.mark.parametrize(
    "subject",
    [
        "Thẻ của bạn sắp hết hạn 20/09",
        "Your card expires on 20 Sep",
        "Chuyến bay của bạn bị hủy",
        "Payment failed for your subscription",
        "Action required: update billing",
        "Hóa đơn đến hạn thanh toán",
    ],
)
def test_a_noreply_sender_with_a_real_deadline_reaches_the_llm(subject):
    # "no-reply" means do not reply to me, NOT this does not matter. This regression
    # test exists because the rule layer silently dropped a bank card-expiry notice.
    assert apply(msg(frm="noreply@bank.com", subject=subject), RULES) is None


@pytest.mark.parametrize(
    "subject",
    ["Weekly digest: 5 things", "New arrivals this week", "Bản tin tháng 9"],
)
def test_a_noreply_sender_with_no_deadline_is_still_ignored(subject):
    d = apply(msg(frm="noreply@shop.com", subject=subject), RULES)
    assert d is not None and d.bucket == Bucket.IGNORE


@pytest.mark.parametrize("local", ["alerta", "systematic", "mailerson", "mai", "boss"])
def test_noreply_pattern_does_not_match_real_people(local):
    assert not NOREPLY_RE.match(local)


# ----------------------------------------------------------------- calendar
def _ev(title, h1, m1, h2, m2, atts=(), **kw) -> CalendarEvent:
    return CalendarEvent(
        id=title,
        calendar_id="primary",
        title=title,
        start=datetime(2026, 9, 12, h1, m1, tzinfo=TZ).astimezone(UTC),
        end=datetime(2026, 9, 12, h2, m2, tzinfo=TZ).astimezone(UTC),
        all_day=kw.get("all_day", False),
        location=None,
        description=None,
        attendees=tuple(atts),
        conference_url=None,
        status="confirmed",
        recurring_event_id=None,
        event_type=kw.get("etype", "default"),
        transparency=kw.get("transp", "opaque"),
        my_response=kw.get("resp"),
    )


def _at(e, **kw) -> Attendee:
    return Attendee(
        email=e,
        name=None,
        response=kw.get("resp", "accepted"),
        is_self=kw.get("me", False),
        resource=kw.get("room", False),
    )


def test_external_count_is_people_not_domains():
    me = _at(ME, me=True)
    e = _ev("call", 9, 0, 10, 0, [me, _at("a@acme.com"), _at("b@acme.com")])
    # Two colleagues from one company is two people to prepare for.
    assert len(e.externals({"mycorp.com"})) == 2


def test_focus_time_blocks_the_slot_but_is_not_meeting_load():
    me = _at(ME, me=True)
    s = analyse(
        [
            _ev("Focus", 9, 0, 11, 0, [me], etype="focusTime"),
            _ev("Standup", 11, 0, 11, 30, [me, _at("t@mycorp.com")]),
        ],
        day=date(2026, 9, 12),
        tz=TZ,
        internal_domains={"mycorp.com"},
    )
    assert s.meeting_minutes == 30  # focus time excluded
    assert len(s.focus_blocks) == 1
    assert all(b.start.hour >= 11 for b in s.free_blocks)  # never offered as free


def test_declined_and_all_day_and_marked_free_do_not_consume_the_day():
    me = _at(ME, me=True)
    s = analyse(
        [
            _ev("declined", 9, 0, 10, 0, [me, _at("x@y.com")], resp="declined"),
            _ev("allday", 0, 0, 23, 59, [me], all_day=True),
            _ev("lunch", 12, 0, 13, 0, [me], transp="transparent"),
        ],
        day=date(2026, 9, 12),
        tz=TZ,
        internal_domains={"mycorp.com"},
    )
    assert s.meeting_minutes == 0
    assert len(s.free_blocks) == 1


def test_free_blocks_never_overlap_a_meeting():
    me = _at(ME, me=True)
    s = analyse(
        [
            _ev("a", 10, 0, 11, 0, [me, _at("x@y.com")]),
            _ev("b", 14, 0, 15, 0, [me, _at("x@y.com")]),
        ],
        day=date(2026, 9, 12),
        tz=TZ,
        internal_domains={"mycorp.com"},
    )
    for b in s.free_blocks:
        assert all(not (b.start < e.end and e.start < b.end) for e in s.events)


def test_short_gaps_are_not_offered_as_deep_work():
    me = _at(ME, me=True)
    s = analyse(
        [
            _ev("a", 9, 0, 10, 0, [me, _at("x@y.com")]),
            _ev("b", 10, 30, 18, 30, [me, _at("x@y.com")]),
        ],
        day=date(2026, 9, 12),
        tz=TZ,
        internal_domains={"mycorp.com"},
    )
    assert s.free_blocks == []  # the only gap is 30 minutes


@pytest.mark.parametrize(
    "given,expect_n",
    [([(9, 10), (10, 11)], 1), ([(9, 12), (10, 11)], 1), ([(9, 10), (11, 12)], 2)],
)
def test_merge(given, expect_n):
    d = lambda h: datetime(2026, 9, 12, h, tzinfo=TZ)
    assert len(merge([(d(a), d(b)) for a, b in given])) == expect_n
