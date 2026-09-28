from __future__ import annotations

import datetime as dt

import pytest

from recall.query import dates

# Monday 28 Sep 2026, 14:00 UTC = 10:00 in New York
MONDAY = dt.datetime(2026, 9, 28, 14, 0, tzinfo=dt.UTC)
NY = "America/New_York"


def test_yesterday_on_monday_is_friday() -> None:
    rng = dates.resolve("yesterday", NY, now=MONDAY)
    assert rng is not None
    assert rng.first_day == rng.last_day == dt.date(2026, 9, 25)
    assert rng.label == "Fri 25 Sep"


def test_yesterday_skips_holidays() -> None:
    rng = dates.resolve("yesterday", NY, holidays=[dt.date(2026, 9, 25)], now=MONDAY)
    assert rng is not None and rng.first_day == dt.date(2026, 9, 24)


def test_last_standup_means_previous_working_day() -> None:
    rng = dates.resolve("last standup", NY, now=MONDAY)
    assert rng is not None and rng.first_day == dt.date(2026, 9, 25)


def test_range_is_in_team_timezone() -> None:
    rng = dates.resolve("yesterday", NY, now=MONDAY)
    assert rng is not None
    assert rng.start == dt.datetime(2026, 9, 25, 4, 0, tzinfo=dt.UTC)  # midnight EDT
    assert rng.end == dt.datetime(2026, 9, 26, 4, 0, tzinfo=dt.UTC)


def test_timezone_changes_today() -> None:
    late_utc = dt.datetime(2026, 9, 29, 2, 0, tzinfo=dt.UTC)  # still Monday evening in New York
    assert dates.local_today(NY, late_utc) == dt.date(2026, 9, 28)
    assert dates.local_today("UTC", late_utc) == dt.date(2026, 9, 29)


@pytest.mark.parametrize(
    ("expr", "first", "last"),
    [
        ("today", dt.date(2026, 9, 28), dt.date(2026, 9, 28)),
        ("last week", dt.date(2026, 9, 21), dt.date(2026, 9, 27)),
        ("this week", dt.date(2026, 9, 28), dt.date(2026, 9, 28)),
        ("last month", dt.date(2026, 8, 1), dt.date(2026, 8, 31)),
        ("tuesday", dt.date(2026, 9, 22), dt.date(2026, 9, 22)),
        ("last monday", dt.date(2026, 9, 21), dt.date(2026, 9, 21)),
        ("this monday", dt.date(2026, 9, 28), dt.date(2026, 9, 28)),
        ("2026-09-02", dt.date(2026, 9, 2), dt.date(2026, 9, 2)),
        ("25 sep", dt.date(2026, 9, 25), dt.date(2026, 9, 25)),
        ("sept 25th", dt.date(2026, 9, 25), dt.date(2026, 9, 25)),
        ("dec 24", dt.date(2025, 12, 24), dt.date(2025, 12, 24)),
        ("last 3 days", dt.date(2026, 9, 25), dt.date(2026, 9, 28)),
    ],
)
def test_expressions(expr: str, first: dt.date, last: dt.date) -> None:
    rng = dates.resolve(expr, NY, now=MONDAY)
    assert rng is not None, expr
    assert (rng.first_day, rng.last_day) == (first, last)


def test_unknown_expression() -> None:
    assert dates.resolve("whenever", NY, now=MONDAY) is None


def test_find_date_expression() -> None:
    assert dates.find_date_expression("What did we cover in yesterday's standup?") == "yesterday"
    assert dates.find_date_expression("what did Priya say last Tuesday") == "last tuesday"
    assert dates.find_date_expression("why did we drop redis") is None


def test_working_days() -> None:
    assert dates.previous_working_day(dt.date(2026, 9, 28)) == dt.date(2026, 9, 25)
    assert dates.next_working_day(dt.date(2026, 9, 25)) == dt.date(2026, 9, 28)
