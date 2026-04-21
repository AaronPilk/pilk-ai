"""SQLite-backed store for one-shot timers.

Contract (used by ``TimerDaemon`` + ``/timers`` routes):

- ``create(fires_at, message)`` — insert a pending row, return the
  :class:`Timer`.
- ``list_active()`` — every row that hasn't fired AND hasn't been
  cancelled. Sorted by ``fires_at ASC`` so the UI can render "next
  up" naturally.
- ``list_recent(limit)`` — everything, active + fired + cancelled,
  newest-first. For the "you've had these reminders" history view.
- ``due_now(now)`` — every active row whose ``fires_at <= now``. The
  daemon polls this.
- ``mark_fired(timer_id, fired_at)`` — SET fired_at conditionally so
  two racing pollers can't deliver the same timer twice. Returns the
  Timer if we claimed it; ``None`` if someone else already did.
- ``cancel(timer_id)`` — set cancelled_at; returns True if the row
  was alive to cancel. Idempotent noop on already-fired / already-
  cancelled rows.
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.db import connect

# Maximum minutes a timer can be set for. Caps what an agent can
# accidentally schedule; if the operator wants a 6-month reminder
# they should use a cron trigger instead.
MAX_TIMER_MINUTES = 24 * 60  # 1 day


def _new_id() -> str:
    return f"tmr_{secrets.token_hex(6)}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Timer:
    id: str
    message: str
    fires_at: str            # ISO UTC
    created_at: str
    fired_at: str | None
    cancelled_at: str | None
    source: str

    def public_dict(self) -> dict:
        return asdict(self)

    @property
    def is_active(self) -> bool:
        return self.fired_at is None and self.cancelled_at is None


class TimerStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def create(
        self,
        *,
        fires_at: datetime,
        message: str,
        source: str = "tool",
    ) -> Timer:
        if not message or not message.strip():
            raise ValueError("timer message must be non-empty")
        now = _now()
        if fires_at <= now:
            raise ValueError("timer fires_at must be in the future")
        delta = fires_at - now
        if delta.total_seconds() > MAX_TIMER_MINUTES * 60:
            raise ValueError(
                f"timer too far out ({delta}; cap "
                f"{MAX_TIMER_MINUTES} min). Use a cron trigger for "
                "longer horizons."
            )
        timer = Timer(
            id=_new_id(),
            message=message.strip(),
            fires_at=fires_at.isoformat(),
            created_at=now.isoformat(),
            fired_at=None,
            cancelled_at=None,
            source=source,
        )
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO timers("
                "id, message, fires_at, created_at, source"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    timer.id,
                    timer.message,
                    timer.fires_at,
                    timer.created_at,
                    timer.source,
                ),
            )
            await conn.commit()
        return timer

    async def list_active(self) -> list[Timer]:
        async with connect(self.db_path) as conn, conn.execute(
            "SELECT id, message, fires_at, created_at, fired_at, "
            "cancelled_at, source "
            "FROM timers "
            "WHERE fired_at IS NULL AND cancelled_at IS NULL "
            "ORDER BY fires_at ASC"
        ) as cur:
            rows = await cur.fetchall()
        return [Timer(**dict(r)) for r in rows]

    async def list_recent(self, limit: int = 50) -> list[Timer]:
        async with connect(self.db_path) as conn, conn.execute(
            "SELECT id, message, fires_at, created_at, fired_at, "
            "cancelled_at, source "
            "FROM timers "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (int(limit),),
        ) as cur:
            rows = await cur.fetchall()
        return [Timer(**dict(r)) for r in rows]

    async def due_now(self, now: datetime | None = None) -> list[Timer]:
        stamp = (now or _now()).isoformat()
        async with connect(self.db_path) as conn, conn.execute(
            "SELECT id, message, fires_at, created_at, fired_at, "
            "cancelled_at, source "
            "FROM timers "
            "WHERE fired_at IS NULL "
            "AND cancelled_at IS NULL "
            "AND fires_at <= ? "
            "ORDER BY fires_at ASC",
            (stamp,),
        ) as cur:
            rows = await cur.fetchall()
        return [Timer(**dict(r)) for r in rows]

    async def mark_fired(
        self, timer_id: str, *, fired_at: datetime | None = None,
    ) -> Timer | None:
        """Atomically claim + mark a timer fired.

        The ``WHERE fired_at IS NULL AND cancelled_at IS NULL``
        clause is what makes this race-safe: two pollers that both
        see the same due row will both try to UPDATE, but only one
        will see ``rowcount == 1``. The other gets ``None`` back +
        skips delivery.
        """
        stamp = (fired_at or _now()).isoformat()
        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "UPDATE timers SET fired_at = ? "
                "WHERE id = ? "
                "AND fired_at IS NULL "
                "AND cancelled_at IS NULL",
                (stamp, timer_id),
            )
            await conn.commit()
            claimed = (cur.rowcount or 0) == 1
            if not claimed:
                return None
            async with conn.execute(
                "SELECT id, message, fires_at, created_at, fired_at, "
                "cancelled_at, source "
                "FROM timers WHERE id = ?",
                (timer_id,),
            ) as sel:
                row = await sel.fetchone()
        return Timer(**dict(row)) if row else None

    async def cancel(self, timer_id: str) -> bool:
        stamp = _now().isoformat()
        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "UPDATE timers SET cancelled_at = ? "
                "WHERE id = ? "
                "AND fired_at IS NULL "
                "AND cancelled_at IS NULL",
                (stamp, timer_id),
            )
            await conn.commit()
            return (cur.rowcount or 0) == 1


__all__ = ["MAX_TIMER_MINUTES", "Timer", "TimerStore"]
