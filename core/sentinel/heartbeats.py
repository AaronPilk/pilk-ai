"""Heartbeat persistence.

Every long-running agent calls :meth:`HeartbeatStore.upsert` (or the
``sentinel_heartbeat`` tool) at least once per ``interval_seconds``.
Sentinel's stale-heartbeat rule reads :meth:`iter_stale` on its 30s
periodic scan.

Rows live in the ``agent_heartbeats`` table (migration v8). Keys are
agent names — at most one heartbeat per agent at a time.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from core.sentinel.contracts import Heartbeat

DEFAULT_INTERVAL_S = 60
DEFAULT_STUCK_TIMEOUT_S = 900


class HeartbeatStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def upsert(
        self,
        *,
        agent_name: str,
        status: str,
        progress: str | None = None,
        active_task_id: str | None = None,
        interval_seconds: int = DEFAULT_INTERVAL_S,
        stuck_task_timeout_seconds: int = DEFAULT_STUCK_TIMEOUT_S,
    ) -> Heartbeat:
        now = datetime.now(UTC).isoformat()
        # 160-char cap on progress — matches the tool schema and keeps
        # one wayward log-line from filling a SQLite row.
        progress_capped = (progress or "")[:160] or None
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO agent_heartbeats(
                       agent_name, status, progress, active_task_id,
                       last_at, interval_seconds, stuck_task_timeout_s
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(agent_name) DO UPDATE SET
                       status = excluded.status,
                       progress = excluded.progress,
                       active_task_id = excluded.active_task_id,
                       last_at = excluded.last_at,
                       interval_seconds = excluded.interval_seconds,
                       stuck_task_timeout_s = excluded.stuck_task_timeout_s""",
                (
                    agent_name,
                    status,
                    progress_capped,
                    active_task_id,
                    now,
                    int(interval_seconds),
                    int(stuck_task_timeout_seconds),
                ),
            )
            conn.commit()
        return Heartbeat(
            agent_name=agent_name,
            status=status,
            progress=progress_capped,
            active_task_id=active_task_id,
            last_at=now,
            interval_seconds=int(interval_seconds),
            stuck_task_timeout_seconds=int(stuck_task_timeout_seconds),
        )

    def get(self, agent_name: str) -> Heartbeat | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT agent_name, status, progress, active_task_id,
                          last_at, interval_seconds, stuck_task_timeout_s
                   FROM agent_heartbeats WHERE agent_name = ?""",
                (agent_name,),
            ).fetchone()
        return _row(row) if row else None

    def list_all(self) -> list[Heartbeat]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT agent_name, status, progress, active_task_id,
                          last_at, interval_seconds, stuck_task_timeout_s
                   FROM agent_heartbeats ORDER BY agent_name"""
            ).fetchall()
        return [_row(r) for r in rows]

    def iter_stale(self, now: datetime | None = None) -> Iterable[Heartbeat]:
        """Yield every heartbeat that's older than 2x its declared
        interval. Agents with status ``disabled`` are exempt (they're
        supposed to be quiet) — Sentinel should not resurrect a
        deliberately disabled agent."""
        now = now or datetime.now(UTC)
        for hb in self.list_all():
            if hb.status == "disabled":
                continue
            last = _parse_iso(hb.last_at)
            if last is None:
                continue
            age = (now - last).total_seconds()
            if age > 2 * hb.interval_seconds:
                # Re-emit with a fresh 'age' annotation so callers have
                # it without re-computing. Kept as a separate kwarg to
                # avoid mutating the frozen dataclass.
                yield replace(hb)

    def delete(self, agent_name: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM agent_heartbeats WHERE agent_name = ?",
                (agent_name,),
            )
            conn.commit()
            return cur.rowcount > 0


def _row(row: tuple) -> Heartbeat:
    return Heartbeat(
        agent_name=row[0],
        status=row[1],
        progress=row[2],
        active_task_id=row[3],
        last_at=row[4],
        interval_seconds=int(row[5]),
        stuck_task_timeout_seconds=int(row[6]),
    )


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_STUCK_TIMEOUT_S",
    "HeartbeatStore",
]
