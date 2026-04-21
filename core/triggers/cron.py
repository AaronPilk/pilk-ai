"""Tiny 5-field cron matcher.

Supports the common POSIX-ish subset, which is plenty for the triggers
V1 ("every weekday at 9am", "every Monday at 07:00", "every hour on the
hour"). No step-of-range (``1-10/2``) and no named months/days — both
can land later without breaking any manifests that stay on the strict
subset.

Each of the five fields supports:

  ``*``                 any value
  ``N``                 a single number in the field's range
  ``N,M,…``             comma-separated list of numbers
  ``N-M``               inclusive range
  ``*/K``               every K-th value (``*/5`` in minutes → every 5m)

Matching runs at minute resolution — the scheduler ticks once a minute.
We deliberately do NOT compute "next fire time"; the scheduler just asks
``schedule.matches(now)`` every tick. Simpler, correctness is obvious,
and it's O(one-digit-microseconds) per check.

The 5 fields, in order:

  minute       (0-59)
  hour         (0-23)
  day of month (1-31)
  month        (1-12)
  day of week  (0-6, Sunday=0)

  ``* * * * *``

Day-of-month vs day-of-week semantics: both must match (AND). POSIX
cron uses OR when either is not ``*`` — that is genuinely surprising
behaviour and almost always not what the operator wants, so we stick
with AND. If the disagreement ever matters we can revisit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

CRON_FIELDS = ("minute", "hour", "dom", "month", "dow")
# (inclusive_low, inclusive_high) for each field in the order above.
CRON_BOUNDS = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 6),
}


class CronParseError(ValueError):
    """Raised when a cron expression is syntactically invalid."""


@dataclass(frozen=True)
class CronSchedule:
    """A parsed 5-field cron expression.

    ``matches(dt)`` answers "should a trigger with this schedule fire
    at wall-clock ``dt``?"  The caller is responsible for tick
    cadence — the scheduler only evaluates once a minute, so the check
    is minute-precise.
    """

    expression: str
    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]

    def matches(self, dt: datetime) -> bool:
        # datetime.weekday: Monday=0 … Sunday=6. Cron convention:
        # Sunday=0 … Saturday=6. Convert once here so the field stays
        # intuitive for a reader of the manifest.
        cron_dow = (dt.weekday() + 1) % 7
        return (
            dt.minute in self.minute
            and dt.hour in self.hour
            and dt.day in self.dom
            and dt.month in self.month
            and cron_dow in self.dow
        )


def parse_cron(expression: str) -> CronSchedule:
    """Parse a 5-field cron expression.

    Raises :class:`CronParseError` on invalid input. The returned
    :class:`CronSchedule` is immutable and hashable so a large set of
    triggers can cheaply share identical schedules.
    """
    if not isinstance(expression, str):
        raise CronParseError(f"cron expression must be a string, got {type(expression)!r}")
    parts = expression.strip().split()
    if len(parts) != 5:
        raise CronParseError(
            f"cron expression must have exactly 5 fields (got {len(parts)}): {expression!r}"
        )
    values: dict[str, frozenset[int]] = {}
    for field, raw in zip(CRON_FIELDS, parts, strict=True):
        low, high = CRON_BOUNDS[field]
        values[field] = frozenset(_parse_field(raw, low=low, high=high, field=field))
    return CronSchedule(expression=expression.strip(), **values)


def _parse_field(raw: str, *, low: int, high: int, field: str) -> set[int]:
    """Expand a single cron field into the explicit integer set it matches."""
    raw = raw.strip()
    if not raw:
        raise CronParseError(f"empty {field} field")
    # Commas split independent sub-expressions.
    out: set[int] = set()
    for piece in raw.split(","):
        out.update(_parse_piece(piece.strip(), low=low, high=high, field=field))
    return out


def _parse_piece(piece: str, *, low: int, high: int, field: str) -> set[int]:
    step = 1
    base = piece
    if "/" in piece:
        base, step_str = piece.split("/", 1)
        try:
            step = int(step_str)
        except ValueError as e:
            raise CronParseError(f"invalid step in {field}: {piece!r}") from e
        if step <= 0:
            raise CronParseError(f"step must be > 0 in {field}: {piece!r}")
    if base == "*":
        start, end = low, high
    elif "-" in base:
        a, b = base.split("-", 1)
        try:
            start, end = int(a), int(b)
        except ValueError as e:
            raise CronParseError(f"invalid range in {field}: {piece!r}") from e
    else:
        try:
            start = end = int(base)
        except ValueError as e:
            raise CronParseError(f"invalid number in {field}: {piece!r}") from e
    if start < low or end > high or start > end:
        raise CronParseError(
            f"{field} value out of range [{low}-{high}]: {piece!r}"
        )
    return set(range(start, end + 1, step))


__all__ = [
    "CRON_BOUNDS",
    "CRON_FIELDS",
    "CronParseError",
    "CronSchedule",
    "parse_cron",
]
