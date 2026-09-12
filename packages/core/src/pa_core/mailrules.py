"""The deterministic rule layer. Shared by project 1 (brief) and project 3 (triage).

This retires 70-85% of a typical inbox at ZERO token cost and -- more importantly --
DETERMINISTICALLY, which means that part of the system never surprises you. An LLM that
quietly reclassifies your newsletters differently on Tuesday is a system you stop
trusting; a regex is not.

Ordered, first hit wins, and every hit is logged BY RULE NAME so you can audit which
rules earn their place. A rule that fires zero times in a month is dead code; one that
fires 60% of the time deserves scrutiny.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .connectors.base import MailMessage


class Bucket(StrEnum):
    NEEDS_ATTENTION = "needs_attention"
    NEEDS_REPLY = "needs_reply"
    IGNORE = "ignore"
    UNCERTAIN = "uncertain"
    SKIP = "skip"  # not about you at all (your own sent mail)


@dataclass(frozen=True, slots=True)
class Decision:
    bucket: Bucket
    confidence: float
    rule: str  # the rule name, or "" when the LLM decided
    reason: str = ""
    draft: bool = False

    @property
    def by_rule(self) -> bool:
        return bool(self.rule)


def ignore(rule: str, conf: float, reason: str = "") -> Decision:
    return Decision(Bucket.IGNORE, conf, rule, reason)


def attention(rule: str, conf: float, reason: str = "", draft: bool = False) -> Decision:
    return Decision(Bucket.NEEDS_ATTENTION, conf, rule, reason, draft)


def skip(rule: str) -> Decision:
    return Decision(Bucket.SKIP, 1.0, rule)


# The trailing `(?:[-_.]?\d+)?\b` matters: real senders are `no-reply2@`, `alerts1@`,
# `mailer-42@`. A bare \b fails on those because a digit is a word character. The
# optional numeric suffix catches them WITHOUT matching a real word like "alerta".
NOREPLY_RE = re.compile(
    r"^(no[-_.]?reply|donotreply|do[-_.]?not[-_.]?reply|notifications?|alerts?|"
    r"mailer|bounce|postmaster|automated|system)(?:[-_.]?\d+)?\b",
    re.I,
)

# A no-reply sender means "do not reply to me", NOT "this does not matter". Bank card
# expiry, flight changes, password resets, legal notices and service outages all arrive
# from noreply@. So the noreply rule must NOT short-circuit to ignore when the subject
# carries a deadline or an action demand -- those go to the LLM like anything else.
# Bilingual, because Vietnamese banks and services write in Vietnamese.
ACTIONABLE_SUBJECT_RE = re.compile(
    r"(?:"
    # English
    r"\bexpir(?:e|es|ed|ing|ation)\b|\brenew(?:al)?\b|\bdue\b|\boverdue\b"
    r"|\bdeadline\b|\blast (?:day|chance|reminder)\b|\bfinal notice\b"
    r"|\baction required\b|\bsuspend(?:ed|ing)?\b|\bdeactivat\w+|\bterminat\w+"
    r"|\bdeclin(?:e|ed)\b|\bfail(?:ed|ure)\b|\bunsuccessful\b|\bdispute\b"
    r"|\bcancel(?:led|lation)?\b|\bdelay(?:ed)?\b|\breschedul\w+|\bchanged?\b"
    r"|\bpassword\b|\bverify your\b|\bconfirm your\b|\bunusual\b|\bbreach\b"
    r"|\binvoice\b|\bpayment (?:due|failed|required)\b|\bbill\b"
    # Vietnamese. Written out literally rather than with clever character classes:
    # `h[uỷ]` looks like it matches "hủy" but is a class matching ONE char, u or ỷ.
    # Diacritics make terse regexes silently wrong, so these are explicit.
    r"|hết hạn|sắp hết hạn|gia hạn|quá hạn|đến hạn|hạn cuối|hạn chót"
    r"|thông báo quan trọng|cảnh báo|khẩn|yêu cầu xác nhận"
    r"|tạm ngưng|tạm dừng|bị khóa|khóa thẻ|bị từ chối|thất bại|không thành công"
    r"|thanh toán|hóa đơn|cước|nợ|dư nợ"
    r"|hủy|đã hủy|bị hủy|thay đổi|dời|trễ|chậm|hoãn"
    r"|đổi mật khẩu|đặt lại mật khẩu|đăng nhập lạ|bất thường"
    r")",
    re.I,
)

OTP_RECEIPT_RE = re.compile(
    r"\b(verification code|one[- ]time (code|password)|\botp\b|security code|"
    r"your receipt|invoice #|payment (received|confirmation|successful)|"
    r"order (confirmed|shipped|placed)|mã xác (thực|minh)|biên nhận)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class RuleConfig:
    internal_domains: frozenset[str] = frozenset()
    vip_domains: frozenset[str] = frozenset()
    vip_addrs: frozenset[str] = frozenset()
    my_email: str = ""
    mute_domains: frozenset[str] = frozenset()


def apply(m: MailMessage, cfg: RuleConfig) -> Decision | None:
    """Returns a Decision, or None meaning "the LLM must look at this one"."""
    h = m.headers

    # VIPs ALWAYS reach the LLM. Never auto-ignored, whatever the other signals say.
    # This rule is first for a reason: one missed message from a real client costs more
    # than every newsletter the rules below will ever catch.
    if m.from_domain in cfg.vip_domains or m.from_addr in cfg.vip_addrs:
        return None

    if cfg.my_email and m.from_addr == cfg.my_email.lower():
        return skip("from_me")

    if m.from_domain in cfg.mute_domains:
        return ignore("muted_domain", 0.99)

    prec = (h.get("precedence") or "").strip().lower()
    if prec in {"bulk", "list", "junk"}:
        return ignore("precedence_bulk", 0.99, f"Precedence: {prec}")

    auto = (h.get("auto-submitted") or "").strip().lower()
    if auto and auto != "no":
        return ignore("auto_submitted", 0.99, f"Auto-Submitted: {auto}")

    to_me_directly = bool(cfg.my_email) and cfg.my_email.lower() in m.to
    if "list-id" in h or ("list-unsubscribe" in h and not to_me_directly):
        return ignore("mailing_list", 0.97, h.get("list-id", "")[:60])

    # CATEGORY_UPDATES is deliberately ABSENT: it holds both "your flight is delayed"
    # and newsletters, so it must go to the LLM.
    if set(m.labels) & {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}:
        cat = next(c for c in m.labels if c.startswith("CATEGORY_"))
        return ignore("gmail_category", 0.96, cat)

    # OTPs and receipts are checked BEFORE the actionable-subject escape hatch:
    # "your receipt" and "payment confirmation" contain payment words but are genuinely
    # finished business, whereas "payment failed" is not.
    if OTP_RECEIPT_RE.search(m.subject):
        return ignore("otp_or_receipt", 0.97, m.subject[:60])

    if NOREPLY_RE.match(m.local_part):
        if ACTIONABLE_SUBJECT_RE.search(m.subject):
            # Deadline or action demand from a no-reply sender -> let the LLM judge it.
            # This is the rule that would otherwise drop a bank card-expiry notice.
            return None
        return ignore("noreply_sender", 0.95, m.from_addr)

    if "text/calendar" in (h.get("content-type") or "").lower():
        return attention("calendar_invite", 0.99, "meeting invite", draft=False)

    return None  # -> the LLM decides


def partition(
    messages: list[MailMessage], cfg: RuleConfig
) -> tuple[list[tuple[MailMessage, Decision]], list[MailMessage]]:
    """Split into (rule-decided, needs-LLM). The second list is what you pay for."""
    decided: list[tuple[MailMessage, Decision]] = []
    ambiguous: list[MailMessage] = []
    for m in messages:
        d = apply(m, cfg)
        if d is None:
            ambiguous.append(m)
        else:
            decided.append((m, d))
    return decided, ambiguous


def rule_histogram(decided: list[tuple[MailMessage, Decision]]) -> dict[str, int]:
    """Feeds the 'Handled without you' section AND the rule audit. A filter that will
    not tell you what it discarded is a filter you stop believing within a week."""
    out: dict[str, int] = {}
    for _, d in decided:
        out[d.rule] = out.get(d.rule, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
