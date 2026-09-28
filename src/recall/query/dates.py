"""Date expressions -> concrete ranges, in the team's timezone, skipping weekends and team holidays.

This lives in code rather than in the LLM so that "yesterday" on a Monday reliably means Friday."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

_WD = "|".join(WEEKDAYS)
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
DATE_EXPRESSION = re.compile(
    rf"\b(today|this morning|yesterday|(?:last|previous|latest|most recent) (?:standup|stand-up|scrum|meeting)"
    rf"|this week|last week|this month|last month|(?:last|past) \d+ (?:days|weeks)"
    rf"|(?:last |this |on )?(?:{_WD})|\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|\d{{1,2}} {_MON}|{_MON} \d{{1,2}}(?:st|nd|rd|th)?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DateRange:
    start: dt.datetime  # inclusive, UTC
    end: dt.datetime  # exclusive, UTC
    first_day: dt.date
    last_day: dt.date
    label: str

    @property
    def scope_key(self) -> str:
        return f"{self.first_day.isoformat()}..{self.last_day.isoformat()}"


def is_working_day(day: dt.date, holidays: Iterable[dt.date] = ()) -> bool:
    return day.weekday() < 5 and day not in set(holidays)


def previous_working_day(day: dt.date, holidays: Iterable[dt.date] = ()) -> dt.date:
    hol = set(holidays)
    candidate = day - dt.timedelta(days=1)
    while not is_working_day(candidate, hol):
        candidate -= dt.timedelta(days=1)
    return candidate


def next_working_day(day: dt.date, holidays: Iterable[dt.date] = ()) -> dt.date:
    hol = set(holidays)
    candidate = day + dt.timedelta(days=1)
    while not is_working_day(candidate, hol):
        candidate += dt.timedelta(days=1)
    return candidate


def local_today(tz: str, now: dt.datetime | None = None) -> dt.date:
    now = now or dt.datetime.now(dt.UTC)
    return now.astimezone(ZoneInfo(tz)).date()


def day_range(first: dt.date, last: dt.date, tz: str, label: str) -> DateRange:
    zone = ZoneInfo(tz)
    start = dt.datetime.combine(first, dt.time.min, zone).astimezone(dt.UTC)
    end = dt.datetime.combine(last + dt.timedelta(days=1), dt.time.min, zone).astimezone(dt.UTC)
    return DateRange(start=start, end=end, first_day=first, last_day=last, label=label)


def find_date_expression(text: str) -> str | None:
    match = DATE_EXPRESSION.search(text)
    return match.group(0).lower() if match else None


def resolve(
    expression: str,
    tz: str = "UTC",
    holidays: Iterable[dt.date] = (),
    now: dt.datetime | None = None,
) -> DateRange | None:
    """Resolve a date expression. Returns None when the expression isn't recognised."""
    hol = list(holidays)
    today = local_today(tz, now)
    expr = re.sub(r"\s+", " ", expression.strip().lower())

    if expr in ("today", "this morning"):
        return day_range(today, today, tz, today.strftime("%a %d %b"))
    if expr == "yesterday" or re.fullmatch(
        r"(?:last|previous|latest|most recent) (?:standup|stand-up|scrum|meeting)", expr
    ):
        day = previous_working_day(today, hol)
        return day_range(day, day, tz, day.strftime("%a %d %b"))
    if expr == "this week":
        monday = today - dt.timedelta(days=today.weekday())
        return day_range(monday, today, tz, "this week")
    if expr == "last week":
        monday = today - dt.timedelta(days=today.weekday() + 7)
        return day_range(monday, monday + dt.timedelta(days=6), tz, "last week")
    if expr == "this month":
        return day_range(today.replace(day=1), today, tz, "this month")
    if expr == "last month":
        last_day = today.replace(day=1) - dt.timedelta(days=1)
        return day_range(last_day.replace(day=1), last_day, tz, "last month")
    match = re.fullmatch(r"(?:last|past) (\d+) (days|weeks)", expr)
    if match:
        days = int(match[1]) * (7 if match[2] == "weeks" else 1)
        return day_range(today - dt.timedelta(days=days), today, tz, expr)
    match = re.fullmatch(rf"(last |this |on )?({_WD})", expr)
    if match:
        delta = (today.weekday() - WEEKDAYS.index(match[2])) % 7
        if (match[1] or "").strip() != "this":
            delta = delta or 7  # "(on|last) Tuesday" asked on a Tuesday means last week's Tuesday
        day = today - dt.timedelta(days=delta)
        return day_range(day, day, tz, day.strftime("%a %d %b"))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expr):
        day = dt.date.fromisoformat(expr)
        return day_range(day, day, tz, day.strftime("%a %d %b"))
    parsed = _parse_day_month(expr, today)
    if parsed is not None:
        return day_range(parsed, parsed, tz, parsed.strftime("%a %d %b"))
    return None


def _parse_day_month(expr: str, today: dt.date) -> dt.date | None:
    match = re.fullmatch(rf"(\d{{1,2}}) ({_MON})", expr) or re.fullmatch(
        rf"({_MON}) (\d{{1,2}})(?:st|nd|rd|th)?", expr
    )
    if not match:
        return None
    a, b = match.groups()
    day_s, month_s = (a, b) if a.isdigit() else (b, a)
    month = MONTHS.index(month_s[:3]) + 1
    try:
        candidate = dt.date(today.year, month, int(day_s))
    except ValueError:
        return None
    if candidate > today:  # "25 Dec" asked in January means last December
        candidate = candidate.replace(year=today.year - 1)
    return candidate
