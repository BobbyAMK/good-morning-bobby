"""Everything about the day that is arithmetic, done as arithmetic.

Free blocks, meeting load, overlaps and external-meeting counts are interval maths. An
LLM computing them would be slower, more expensive, and occasionally wrong -- and on the
zero-cost path, every token handed to a 7B model is a token it can get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from pa_core.connectors.base import CalendarEvent


@dataclass(frozen=True, slots=True)
class Block:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def human(self, tz: ZoneInfo) -> str:
        s, e = self.start.astimezone(tz), self.end.astimezone(tz)
        h, m = divmod(self.minutes, 60)
        return f"{s:%H:%M}–{e:%H:%M} ({h}h{m:02d}m)"


@dataclass(slots=True)
class DayShape:
    day: date
    events: list[CalendarEvent] = field(default_factory=list)
    free_blocks: list[Block] = field(default_factory=list)
    # Focus blocks you ALREADY defended are tracked apart from meeting load: they
    # occupy the calendar (so they are not offered as free) but they are not meetings,
    # and conflating them would make a well-protected day look overbooked.
    focus_blocks: list[Block] = field(default_factory=list)
    meeting_minutes: int = 0
    external_count: int = 0
    overlaps: list[tuple[str, str]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def booked_human(self) -> str:
        h, m = divmod(self.meeting_minutes, 60)
        return f"{h}h{m:02d}m"

    @property
    def deep_work_minutes(self) -> int:
        return sum(b.minutes for b in self.free_blocks) + sum(b.minutes for b in self.focus_blocks)


def occupies_calendar(ev: CalendarEvent) -> bool:
    """Does this event block the slot? Focus time DOES -- you defended it on purpose,
    so the brief must not turn around and offer it to you as free."""
    if ev.all_day:
        return False
    if ev.transparency == "transparent":  # marked Free
        return False
    if ev.event_type in {"outOfOffice", "workingLocation", "birthday"}:
        return False
    if ev.my_response == "declined":
        return False
    return True


def is_meeting(ev: CalendarEvent) -> bool:
    """Counts toward meeting load. Focus time and solo blocks do not."""
    return occupies_calendar(ev) and ev.event_type == "default" and bool(ev.others())


# kept as an alias: "busy" is ambiguous, which is what caused the bug
is_busy = occupies_calendar


def merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    out = [intervals[0]]
    for s, e in sorted(intervals)[1:]:
        ls, le = out[-1]
        if s <= le:
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


def find_overlaps(events: list[CalendarEvent]) -> list[tuple[str, str]]:
    busy = sorted([e for e in events if occupies_calendar(e)], key=lambda e: e.start)
    out = []
    for i, a in enumerate(busy):
        for b in busy[i + 1 :]:
            if b.start >= a.end:
                break
            out.append((a.title, b.title))
    return out


def analyse(
    events: list[CalendarEvent],
    *,
    day: date,
    tz: ZoneInfo,
    internal_domains: set[str],
    work_start: time = time(9, 0),
    work_end: time = time(18, 30),
    min_free_minutes: int = 45,
) -> DayShape:
    """Free blocks by interval subtraction, within working hours, in LOCAL time."""
    todays = [e for e in events if e.start.astimezone(tz).date() == day]
    occupying = [e for e in todays if occupies_calendar(e)]
    meetings = [e for e in occupying if is_meeting(e)]

    day_start = datetime.combine(day, work_start, tzinfo=tz)
    day_end = datetime.combine(day, work_end, tzinfo=tz)

    busy = merge(
        [
            (max(e.start.astimezone(tz), day_start), min(e.end.astimezone(tz), day_end))
            for e in occupying
            if e.end.astimezone(tz) > day_start and e.start.astimezone(tz) < day_end
        ]
    )

    free: list[Block] = []
    cursor = day_start
    for s, e in busy:
        if (s - cursor).total_seconds() / 60 >= min_free_minutes:
            free.append(Block(cursor, s))
        cursor = max(cursor, e)
    if (day_end - cursor).total_seconds() / 60 >= min_free_minutes:
        free.append(Block(cursor, day_end))

    focus = [
        Block(e.start.astimezone(tz), e.end.astimezone(tz))
        for e in occupying
        if e.event_type == "focusTime"
    ]

    skipped: dict[str, int] = {}
    for e in todays:
        if e in occupying:
            if e in meetings and not e.externals(internal_domains):
                skipped["internal_only"] = skipped.get("internal_only", 0) + 1
        else:
            key = (
                "all_day"
                if e.all_day
                else "declined"
                if e.my_response == "declined"
                else "marked_free"
                if e.transparency == "transparent"
                else e.event_type
            )
            skipped[key] = skipped.get(key, 0) + 1

    return DayShape(
        day=day,
        events=occupying,
        free_blocks=free,
        focus_blocks=focus,
        meeting_minutes=sum(int((e.end - e.start).total_seconds() // 60) for e in meetings),
        external_count=sum(1 for e in meetings if e.externals(internal_domains)),
        overlaps=find_overlaps(occupying),
        skipped=skipped,
    )


def render_agenda(shape: DayShape, tz: ZoneInfo, internal_domains: set[str]) -> list[str]:
    lines = []
    for e in shape.events:
        s, en = e.start.astimezone(tz), e.end.astimezone(tz)
        ext = e.externals(internal_domains)  # PEOPLE, not domains
        mark = "⚑" if ext else " "
        extra = f" ({len(ext)} external)" if ext else ""
        if e.recurring_event_id and not ext:
            extra += " (recurring)"
        lines.append(f" {s:%H:%M}–{en:%H:%M}  {mark} {e.title}{extra}")
    return lines
