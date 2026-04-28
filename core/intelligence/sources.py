"""Source registry — CRUD over the ``intel_sources`` table.

Each source is a configured external feed/URL PILK watches. The
registry is the single read/write surface; HTTP routes call into it
and never touch the table directly. Same async/aiosqlite pattern as
the rest of the codebase (timers, agent_policies, etc.).

Batch 1: no daemon polls these — items only land when the operator
hits the manual refresh endpoint or the test endpoint. The
``enabled``, ``mute_until``, and ``poll_interval_seconds`` fields are
stored faithfully for a future daemon to consume; they don't drive
behaviour yet.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.db import connect
from core.intelligence.models import Priority, SourceKind, SourceSpec
from core.logging import get_logger

log = get_logger("pilkd.intelligence.sources")

# Slug rules: lowercase letters, digits, hyphens. 1-64 chars. Stable
# enough to embed in URLs, narrow enough to prevent collisions /
# weird filesystem corner cases.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Source kinds we ACCEPT into the registry. ``manual`` is operator-
# curated — items only land via the dedicated POST /items endpoint
# (Batch 3C). The daemon never polls it. All other kinds are
# network-fetched.
_VALID_KINDS = {
    "rss",
    "json_api",
    "html",
    "github_releases",
    "hacker_news",
    "arxiv",
    "youtube",
    "reddit",
    "x",
    "custom",
    "manual",
}

_VALID_PRIORITIES = {"low", "medium", "high", "critical"}


# Sentinel to distinguish "caller didn't pass this kwarg" from
# "caller passed None to clear the field". Module-level so the
# default in ``update()`` binds at import time.
class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


_UNSET: Any = _Unset()


class SourceValidationError(ValueError):
    """Raised when an operator submits an invalid source definition."""


class SourceRegistry:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # ── reads ────────────────────────────────────────────────────

    async def list_sources(
        self,
        *,
        enabled_only: bool = False,
        project_slug: str | None = None,
    ) -> list[SourceSpec]:
        sql = "SELECT * FROM intel_sources"
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if project_slug is not None:
            clauses.append("project_slug = ?")
            params.append(project_slug)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"
        async with connect(self.db_path) as conn:
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [self._row_to_spec(r) for r in rows]

    async def get(self, source_id: str) -> SourceSpec | None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM intel_sources WHERE id = ?", (source_id,),
            ) as cur:
                row = await cur.fetchone()
        return self._row_to_spec(row) if row else None

    async def get_by_slug(self, slug: str) -> SourceSpec | None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM intel_sources WHERE slug = ?", (slug,),
            ) as cur:
                row = await cur.fetchone()
        return self._row_to_spec(row) if row else None

    # ── writes ───────────────────────────────────────────────────

    async def create(
        self,
        *,
        slug: str,
        kind: SourceKind,
        label: str,
        url: str,
        config: dict[str, Any] | None = None,
        enabled: bool = True,
        default_priority: Priority = "medium",
        project_slug: str | None = None,
        poll_interval_seconds: int = 3600,
    ) -> SourceSpec:
        slug = self._validate_slug(slug)
        kind_v = self._validate_kind(kind)
        label_v = self._validate_label(label)
        url_v = self._validate_url(url)
        priority_v = self._validate_priority(default_priority)
        poll = self._validate_poll(poll_interval_seconds)
        existing = await self.get_by_slug(slug)
        if existing is not None:
            raise SourceValidationError(
                f"a source with slug '{slug}' already exists"
            )
        new_id = "src_" + secrets.token_hex(8)
        now = datetime.now(UTC).isoformat()
        config_json = json.dumps(config or {}, separators=(",", ":"))
        async with connect(self.db_path) as conn:
            await conn.execute(
                """INSERT INTO intel_sources(
                    id, slug, kind, label, url, config_json,
                    enabled, default_priority, project_slug,
                    poll_interval_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id, slug, kind_v, label_v, url_v, config_json,
                    1 if enabled else 0, priority_v, project_slug,
                    poll, now, now,
                ),
            )
            await conn.commit()
        log.info(
            "intel_source_created",
            source_id=new_id, slug=slug, kind=kind_v,
        )
        spec = await self.get(new_id)
        assert spec is not None  # just inserted
        return spec

    async def update(
        self,
        source_id: str,
        *,
        label: str | None = None,
        url: str | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool | None = None,
        default_priority: Priority | None = None,
        project_slug: Any = _UNSET,
        poll_interval_seconds: int | None = None,
        mute_until: Any = _UNSET,
    ) -> SourceSpec | None:
        existing = await self.get(source_id)
        if existing is None:
            return None
        sets: list[str] = []
        params: list[Any] = []
        if label is not None:
            sets.append("label = ?")
            params.append(self._validate_label(label))
        if url is not None:
            sets.append("url = ?")
            params.append(self._validate_url(url))
        if config is not None:
            sets.append("config_json = ?")
            params.append(json.dumps(config, separators=(",", ":")))
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(1 if enabled else 0)
        if default_priority is not None:
            sets.append("default_priority = ?")
            params.append(self._validate_priority(default_priority))
        if not isinstance(project_slug, _Unset):
            sets.append("project_slug = ?")
            params.append(project_slug)
        if poll_interval_seconds is not None:
            sets.append("poll_interval_seconds = ?")
            params.append(self._validate_poll(poll_interval_seconds))
        if not isinstance(mute_until, _Unset):
            sets.append("mute_until = ?")
            params.append(mute_until)
        if not sets:
            return existing
        sets.append("updated_at = ?")
        params.append(datetime.now(UTC).isoformat())
        params.append(source_id)
        async with connect(self.db_path) as conn:
            await conn.execute(
                f"UPDATE intel_sources SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            await conn.commit()
        log.info("intel_source_updated", source_id=source_id)
        return await self.get(source_id)

    async def delete(self, source_id: str) -> bool:
        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "DELETE FROM intel_sources WHERE id = ?", (source_id,),
            )
            await conn.commit()
            removed = cur.rowcount or 0
        if removed:
            log.info("intel_source_deleted", source_id=source_id)
        return removed > 0

    async def record_fetch_outcome(
        self,
        source_id: str,
        *,
        status: str,
        etag: str | None = None,
        last_modified: str | None = None,
        increment_failures: bool = False,
        reset_failures: bool = False,
    ) -> None:
        """Update the source row after a fetch attempt. Pure
        bookkeeping — never moves item rows or rewrites history."""
        now = datetime.now(UTC).isoformat()
        sets = ["last_checked_at = ?", "last_status = ?", "updated_at = ?"]
        params: list[Any] = [now, status, now]
        if etag is not None:
            sets.append("etag = ?")
            params.append(etag)
        if last_modified is not None:
            sets.append("last_modified = ?")
            params.append(last_modified)
        if reset_failures:
            sets.append("consecutive_failures = 0")
        elif increment_failures:
            sets.append("consecutive_failures = consecutive_failures + 1")
        params.append(source_id)
        async with connect(self.db_path) as conn:
            await conn.execute(
                f"UPDATE intel_sources SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            await conn.commit()

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _validate_slug(slug: str) -> str:
        s = (slug or "").strip().lower()
        if not _SLUG_RE.match(s):
            raise SourceValidationError(
                f"invalid slug '{slug}' — use lowercase letters, "
                "digits, and hyphens (1-64 chars, starts with letter/digit)"
            )
        return s

    @staticmethod
    def _validate_kind(kind: str) -> str:
        if kind not in _VALID_KINDS:
            raise SourceValidationError(
                f"invalid source kind '{kind}'. Valid kinds: "
                f"{sorted(_VALID_KINDS)}"
            )
        return kind

    @staticmethod
    def _validate_label(label: str) -> str:
        s = (label or "").strip()
        if not s:
            raise SourceValidationError("label is required")
        if len(s) > 200:
            raise SourceValidationError("label cannot exceed 200 chars")
        return s

    @staticmethod
    def _validate_url(url: str) -> str:
        s = (url or "").strip()
        if not s:
            raise SourceValidationError("url is required")
        if not (s.startswith("http://") or s.startswith("https://")):
            raise SourceValidationError(
                f"url must start with http:// or https:// — got {url!r}"
            )
        if len(s) > 2048:
            raise SourceValidationError("url cannot exceed 2048 chars")
        return s

    @staticmethod
    def _validate_priority(priority: str) -> str:
        if priority not in _VALID_PRIORITIES:
            raise SourceValidationError(
                f"invalid priority '{priority}'. Valid: "
                f"{sorted(_VALID_PRIORITIES)}"
            )
        return priority

    @staticmethod
    def _validate_poll(seconds: int) -> int:
        try:
            v = int(seconds)
        except (TypeError, ValueError) as e:
            raise SourceValidationError(
                "poll_interval_seconds must be an integer"
            ) from e
        # Floor at 60s to avoid hammering sources; ceiling at 7 days.
        if v < 60:
            raise SourceValidationError(
                "poll_interval_seconds floor is 60 (1 minute)"
            )
        if v > 604_800:
            raise SourceValidationError(
                "poll_interval_seconds ceiling is 604800 (7 days)"
            )
        return v

    @staticmethod
    def _row_to_spec(row: Any) -> SourceSpec:
        config = {}
        raw = row["config_json"]
        if raw:
            try:
                config = json.loads(raw)
            except json.JSONDecodeError:
                config = {}
        return SourceSpec(
            id=row["id"],
            slug=row["slug"],
            kind=row["kind"],
            label=row["label"],
            url=row["url"],
            config=config,
            enabled=bool(row["enabled"]),
            default_priority=row["default_priority"],
            project_slug=row["project_slug"],
            poll_interval_seconds=row["poll_interval_seconds"],
            last_checked_at=row["last_checked_at"],
            last_status=row["last_status"],
            consecutive_failures=row["consecutive_failures"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            mute_until=row["mute_until"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


__all__ = ["SourceRegistry", "SourceValidationError"]
