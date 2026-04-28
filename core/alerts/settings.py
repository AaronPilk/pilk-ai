"""Operator-tunable alert settings, stored in ``alert_settings_kv``.

Singleton-style key/value rows. Defaults are conservative:

  - ``telegram_enabled = False``        — proactive Telegram is OFF
  - ``daily_brief_scheduled = False``   — no auto-fire daily brief
  - ``weekly_brief_scheduled = False``  — no auto-fire weekly brief
  - ``digest_only = True``              — alerts route to digest by
                                           default (no push channels)
  - ``max_per_day = 10``                — daily cap on push alerts
  - ``min_score = 70``                  — relevance floor for push

Operator changes settings via the API. The router reads through
this object on every routing decision so changes take effect
immediately — no daemon restart needed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.db import connect


# Default values. Kept as a dict so the test surface can introspect
# them without poking at field defaults.
DEFAULTS: dict[str, Any] = {
    "telegram_enabled": False,
    "daily_brief_scheduled": False,
    "weekly_brief_scheduled": False,
    "digest_only": True,
    "max_per_day": 10,
    "min_score": 70,
    # quiet_hours overrides ``settings.quiet_hours_local`` when set
    # (empty string => use the global setting).
    "quiet_hours": "",
}


@dataclass
class AlertSettingsSnapshot:
    """Read-only snapshot returned to callers + serialized over HTTP."""

    telegram_enabled: bool = False
    daily_brief_scheduled: bool = False
    weekly_brief_scheduled: bool = False
    digest_only: bool = True
    max_per_day: int = 10
    min_score: int = 70
    quiet_hours: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AlertSettings:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    async def get(self) -> AlertSettingsSnapshot:
        """Resolve every setting, falling back to ``DEFAULTS`` for
        keys not yet in the table. Returns a frozen snapshot."""
        rows: dict[str, str] = {}
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT key, value FROM alert_settings_kv"
            ) as cur:
                for r in await cur.fetchall():
                    rows[r[0]] = r[1]
        merged: dict[str, Any] = dict(DEFAULTS)
        for k, raw in rows.items():
            if k not in DEFAULTS:
                continue  # unknown keys are ignored, not crashing
            default_val = DEFAULTS[k]
            try:
                if isinstance(default_val, bool):
                    merged[k] = raw == "true"
                elif isinstance(default_val, int):
                    merged[k] = int(raw)
                else:
                    merged[k] = raw
            except (TypeError, ValueError):
                merged[k] = default_val
        return AlertSettingsSnapshot(**merged)

    async def update(self, **changes: Any) -> AlertSettingsSnapshot:
        """Apply partial updates. Unknown keys are rejected with a
        ``ValueError`` so a typo doesn't silently no-op."""
        if not changes:
            return await self.get()
        for k in changes:
            if k not in DEFAULTS:
                raise ValueError(
                    f"unknown alert setting: {k!r} "
                    f"(valid keys: {sorted(DEFAULTS)})"
                )
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            for k, v in changes.items():
                if isinstance(DEFAULTS[k], bool):
                    raw = "true" if bool(v) else "false"
                elif isinstance(DEFAULTS[k], int):
                    raw = str(int(v))
                else:
                    raw = str(v) if v is not None else ""
                await conn.execute(
                    "INSERT INTO alert_settings_kv(key, value, updated_at) "
                    "VALUES(?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, updated_at=excluded.updated_at",
                    (k, raw, now),
                )
            await conn.commit()
        return await self.get()


@dataclass
class TopicOverride:
    """Per-topic override carried in ``alert_topic_overrides``.

    ``mode`` is one of:
      - ``digest``  — include in the digest, never push (default)
      - ``push``    — push via enabled channels (Telegram if on)
      - ``mute``    — drop entirely
    """

    topic_slug: str
    mode: str = "digest"
    mute_until: str | None = None
    updated_at: str = ""


class TopicOverrideStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    async def list(self) -> list[TopicOverride]:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT topic_slug, mode, mute_until, updated_at "
                "FROM alert_topic_overrides ORDER BY topic_slug"
            ) as cur:
                rows = await cur.fetchall()
        return [
            TopicOverride(
                topic_slug=r[0], mode=r[1] or "digest",
                mute_until=r[2], updated_at=r[3] or "",
            )
            for r in rows
        ]

    async def get(self, topic_slug: str) -> TopicOverride | None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT topic_slug, mode, mute_until, updated_at "
                "FROM alert_topic_overrides WHERE topic_slug = ?",
                (topic_slug,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return TopicOverride(
            topic_slug=row[0], mode=row[1] or "digest",
            mute_until=row[2], updated_at=row[3] or "",
        )

    async def upsert(
        self,
        *,
        topic_slug: str,
        mode: str,
        mute_until: str | None = None,
    ) -> TopicOverride:
        if mode not in ("digest", "push", "mute"):
            raise ValueError(
                f"invalid mode {mode!r} (use digest/push/mute)"
            )
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO alert_topic_overrides("
                "topic_slug, mode, mute_until, updated_at) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(topic_slug) DO UPDATE SET "
                "mode=excluded.mode, mute_until=excluded.mute_until, "
                "updated_at=excluded.updated_at",
                (topic_slug, mode, mute_until, now),
            )
            await conn.commit()
        return TopicOverride(
            topic_slug=topic_slug, mode=mode,
            mute_until=mute_until, updated_at=now,
        )


__all__ = [
    "AlertSettings",
    "AlertSettingsSnapshot",
    "DEFAULTS",
    "TopicOverride",
    "TopicOverrideStore",
]
