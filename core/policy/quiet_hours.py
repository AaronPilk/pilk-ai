"""Quiet-hours policy for proactive, unsolicited PILK outreach.

Proactive triggers (``proactive_checkin``, approval-waiting pings,
sentinel nudges) should not wake the operator at 3am. Replies to
operator-initiated messages always go through — quiet hours is about
unsolicited pings, not conversation.

### Format

Env / settings value is a time-range string in 24-hour form:

    "22:00-08:00"   # spans midnight — 10pm through 8am local
    "00:00-06:00"   # early morning only
    "off"           # disabled

Empty / unset / "off" means "no quiet hours; proactive comms are
always allowed." The default is ``22:00-08:00`` in the operator's
local timezone so the daemon has sensible behaviour on first boot
without config.

### Timezone

We use the operator's local tz (``settings.quiet_hours_tz``), not
UTC. Reason: quiet hours are about when the operator is asleep, which
is a local-time concept. Falls back to the daemon's system zoneinfo
if the setting is empty or invalid.

### API

One function the callers need:

    is_quiet(now: datetime | None = None) -> bool

Returns True when ``now`` falls inside the configured window.
Callers keep the rest of the logic (send anyway on ``urgent=True``,
queue for later, etc.) so this module stays a pure policy check.
"""

from __future__ import annotations

import re
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.config import get_settings
from core.logging import get_logger

log = get_logger("pilkd.policy.quiet_hours")

_RANGE_RE = re.compile(
    r"^\s*(?P<sh>\d{1,2}):(?P<sm>\d{2})"
    r"\s*-\s*"
    r"(?P<eh>\d{1,2}):(?P<em>\d{2})\s*$"
)

# Fallback when the configured tz is missing / typoed. Kept as a
# constant so tests can patch and so the log line is actionable.
_FALLBACK_TZ_NAME = "UTC"


def _parse_range(raw: str) -> tuple[time, time] | None:
    """Parse ``HH:MM-HH:MM`` → (start, end). Returns None on bad input.

    ``"off"`` / empty / unparseable → None (quiet hours disabled).
    Times are normalised to ``time`` objects in the operator's local
    tz — no date component, so the caller handles the wraparound
    case where the window crosses midnight.
    """
    if not raw:
        return None
    cleaned = raw.strip().lower()
    if cleaned in ("off", "none", "disabled", ""):
        return None
    m = _RANGE_RE.match(raw)
    if m is None:
        log.warning("quiet_hours_unparseable", raw=raw)
        return None
    sh, sm = int(m.group("sh")), int(m.group("sm"))
    eh, em = int(m.group("eh")), int(m.group("em"))
    if not (0 <= sh < 24 and 0 <= sm < 60 and 0 <= eh < 24 and 0 <= em < 60):
        log.warning("quiet_hours_out_of_range", raw=raw)
        return None
    return time(sh, sm), time(eh, em)


def _resolve_tz(name: str | None) -> ZoneInfo:
    """Return a ZoneInfo for ``name`` or the UTC fallback.

    Bad tz names on first-boot configs shouldn't crash the daemon —
    log once and fall back so the rest of the pipeline keeps running.
    """
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            log.warning("quiet_hours_bad_tz", tz=name)
    return ZoneInfo(_FALLBACK_TZ_NAME)


def is_quiet(now: datetime | None = None) -> bool:
    """True when ``now`` falls inside the operator's quiet-hours window.

    Reads the range + tz from ``Settings`` each call. Live config
    reloads aren't wired up yet, but the function is cheap so we pay
    the lookup rather than cache across restarts.

    Caller-supplied ``now`` can be any tz-aware datetime (will be
    converted) or None (use the current time in the operator's
    configured tz).
    """
    settings = get_settings()
    parsed = _parse_range(settings.quiet_hours_local)
    if parsed is None:
        return False
    start, end = parsed
    tz = _resolve_tz(settings.quiet_hours_tz)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    current = moment.time()
    if start <= end:
        # Same-day window (e.g. 02:00-06:00).
        return start <= current < end
    # Wraps midnight (e.g. 22:00-08:00): inside if >= start OR < end.
    return current >= start or current < end


def describe() -> str:
    """Short human-readable summary for logs / system-prompt use."""
    settings = get_settings()
    parsed = _parse_range(settings.quiet_hours_local)
    if parsed is None:
        return "quiet_hours=off"
    start, end = parsed
    return (
        f"quiet_hours={start.strftime('%H:%M')}-{end.strftime('%H:%M')} "
        f"tz={settings.quiet_hours_tz or _FALLBACK_TZ_NAME}"
    )


__all__ = ["describe", "is_quiet"]
