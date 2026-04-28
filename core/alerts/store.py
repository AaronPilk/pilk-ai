"""Append-only event log for alerts.

Every routing decision writes one row to ``alerts`` so the
dashboard / audit trail can replay why a particular item was
silent vs digest vs pushed. Includes the dedupe fingerprint so
duplicate-suppression is auditable.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.db import connect


def _uid() -> str:
    return f"alt_{secrets.token_hex(8)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class AlertEvent:
    """Read-back shape returned by ``AlertStore.list_recent``."""

    id: str
    kind: str
    severity: str
    title: str
    body: str | None
    project_slug: str | None
    topic_slug: str | None
    source_slug: str | None
    score: int | None
    dedupe_key: str
    delivery: str  # silent|digest|dashboard|telegram
    delivered_at: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


class AlertStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    async def insert(
        self,
        *,
        kind: str,
        title: str,
        delivery: str,
        dedupe_key: str,
        body: str | None = None,
        severity: str = "info",
        project_slug: str | None = None,
        topic_slug: str | None = None,
        source_slug: str | None = None,
        score: int | None = None,
        delivered_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AlertEvent:
        if delivery not in ("silent", "digest", "dashboard", "telegram"):
            raise ValueError(
                f"invalid delivery {delivery!r} "
                f"(use silent/digest/dashboard/telegram)"
            )
        aid = _uid()
        now = _now()
        meta_raw = json.dumps(metadata or {})
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO alerts("
                "id, kind, severity, title, body, project_slug, "
                "topic_slug, source_slug, score, dedupe_key, "
                "delivery, delivered_at, metadata_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid, kind, severity, title, body, project_slug,
                    topic_slug, source_slug, score, dedupe_key,
                    delivery, delivered_at, meta_raw, now,
                ),
            )
            await conn.commit()
        return AlertEvent(
            id=aid, kind=kind, severity=severity, title=title,
            body=body, project_slug=project_slug,
            topic_slug=topic_slug, source_slug=source_slug,
            score=score, dedupe_key=dedupe_key, delivery=delivery,
            delivered_at=delivered_at,
            metadata=metadata or {}, created_at=now,
        )

    async def already_seen(
        self, dedupe_key: str, *, within_hours: int = 24,
    ) -> bool:
        """Returns True if an alert with the same ``dedupe_key`` was
        recorded in the last ``within_hours``. Used for duplicate
        suppression — we don't double-fire the same item even if
        the upstream pipeline re-classifies it."""
        cutoff = (
            datetime.now(UTC) - timedelta(hours=within_hours)
        ).isoformat()
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT 1 FROM alerts "
                "WHERE dedupe_key = ? AND created_at >= ? LIMIT 1",
                (dedupe_key, cutoff),
            ) as cur:
                row = await cur.fetchone()
        return row is not None

    async def count_pushes_today(self) -> int:
        """Count Telegram pushes recorded so far today (UTC). The
        router uses this to enforce ``max_per_day``."""
        start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM alerts "
                "WHERE delivery = 'telegram' AND created_at >= ?",
                (start,),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0] or 0) if row else 0

    async def list_recent(
        self,
        *,
        limit: int = 50,
        delivery: str | None = None,
        kind: str | None = None,
    ) -> list[AlertEvent]:
        wheres: list[str] = []
        params: list = []
        if delivery is not None:
            wheres.append("delivery = ?")
            params.append(delivery)
        if kind is not None:
            wheres.append("kind = ?")
            params.append(kind)
        where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(int(limit))
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT id, kind, severity, title, body, project_slug, "
                "topic_slug, source_slug, score, dedupe_key, delivery, "
                "delivered_at, metadata_json, created_at FROM alerts"
                + where_sql + " ORDER BY created_at DESC LIMIT ?",
                params,
            ) as cur:
                rows = await cur.fetchall()
        out: list[AlertEvent] = []
        for r in rows:
            meta_raw = r[12] or "{}"
            try:
                meta = json.loads(meta_raw)
            except json.JSONDecodeError:
                meta = {}
            out.append(
                AlertEvent(
                    id=r[0], kind=r[1], severity=r[2], title=r[3],
                    body=r[4], project_slug=r[5], topic_slug=r[6],
                    source_slug=r[7], score=r[8], dedupe_key=r[9],
                    delivery=r[10], delivered_at=r[11], metadata=meta,
                    created_at=r[13] or "",
                )
            )
        return out


__all__ = ["AlertEvent", "AlertStore"]
