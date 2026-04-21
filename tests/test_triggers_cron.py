"""Tests for the 5-field cron matcher.

The scheduler asks ``schedule.matches(dt)`` once a minute, so these
tests are minute-precision. We deliberately don't cover "next fire
time" computation — the scheduler doesn't need it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.triggers.cron import CronParseError, parse_cron

# ── wildcard + basic matching ───────────────────────────────────


def test_wildcard_matches_every_minute() -> None:
    c = parse_cron("* * * * *")
    assert c.matches(datetime(2026, 4, 21, 13, 37, tzinfo=UTC))
    assert c.matches(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))


def test_fixed_minute_hour() -> None:
    c = parse_cron("0 7 * * *")
    assert c.matches(datetime(2026, 4, 21, 7, 0, tzinfo=UTC))
    assert not c.matches(datetime(2026, 4, 21, 7, 1, tzinfo=UTC))
    assert not c.matches(datetime(2026, 4, 21, 8, 0, tzinfo=UTC))


# ── lists, ranges, steps ────────────────────────────────────────


def test_list_field() -> None:
    c = parse_cron("0,15,30,45 * * * *")
    for minute in (0, 15, 30, 45):
        assert c.matches(datetime(2026, 4, 21, 10, minute, tzinfo=UTC))
    assert not c.matches(datetime(2026, 4, 21, 10, 16, tzinfo=UTC))


def test_range_field() -> None:
    c = parse_cron("0 9-17 * * *")
    assert c.matches(datetime(2026, 4, 21, 9, 0, tzinfo=UTC))
    assert c.matches(datetime(2026, 4, 21, 17, 0, tzinfo=UTC))
    assert not c.matches(datetime(2026, 4, 21, 8, 0, tzinfo=UTC))
    assert not c.matches(datetime(2026, 4, 21, 18, 0, tzinfo=UTC))


def test_step_over_wildcard() -> None:
    c = parse_cron("*/15 * * * *")
    for minute in (0, 15, 30, 45):
        assert c.matches(datetime(2026, 4, 21, 10, minute, tzinfo=UTC))
    for minute in (1, 14, 16, 29, 31):
        assert not c.matches(datetime(2026, 4, 21, 10, minute, tzinfo=UTC))


def test_step_over_range() -> None:
    # Every 2nd hour between 0-10 (inclusive): 0, 2, 4, 6, 8, 10.
    c = parse_cron("0 0-10/2 * * *")
    for hour in (0, 2, 4, 6, 8, 10):
        assert c.matches(datetime(2026, 4, 21, hour, 0, tzinfo=UTC))
    for hour in (1, 3, 11, 12):
        assert not c.matches(datetime(2026, 4, 21, hour, 0, tzinfo=UTC))


# ── day-of-week (cron Sunday=0) ─────────────────────────────────


def test_dow_weekdays_only() -> None:
    # Monday through Friday; datetime.weekday: Mon=0 … Sun=6, cron: Sun=0 … Sat=6
    c = parse_cron("0 9 * * 1-5")
    # 2026-04-20 is a Monday.
    assert c.matches(datetime(2026, 4, 20, 9, 0, tzinfo=UTC))
    # 2026-04-24 is a Friday.
    assert c.matches(datetime(2026, 4, 24, 9, 0, tzinfo=UTC))
    # 2026-04-25 is a Saturday.
    assert not c.matches(datetime(2026, 4, 25, 9, 0, tzinfo=UTC))
    # 2026-04-26 is a Sunday.
    assert not c.matches(datetime(2026, 4, 26, 9, 0, tzinfo=UTC))


def test_dow_sunday_is_zero() -> None:
    # Cron's Sunday=0 convention — critical this survives the weekday
    # conversion.
    c = parse_cron("0 12 * * 0")
    assert c.matches(datetime(2026, 4, 26, 12, 0, tzinfo=UTC))  # Sunday
    assert not c.matches(datetime(2026, 4, 27, 12, 0, tzinfo=UTC))  # Monday


# ── parse errors ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "* * *",                 # too few fields
        "* * * * * *",           # too many
        "60 * * * *",            # minute out of range
        "* 24 * * *",            # hour out of range
        "* * 32 * *",            # dom out of range
        "* * * 13 *",            # month out of range
        "* * * * 7",             # dow out of range
        "*/0 * * * *",           # zero step
        "abc * * * *",           # non-numeric
        "5-3 * * * *",           # inverted range
    ],
)
def test_parse_errors(expr: str) -> None:
    with pytest.raises(CronParseError):
        parse_cron(expr)
