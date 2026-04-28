"""Watchlist topics — CRUD over the ``intel_topics`` table.

Topics are operator-defined "things I care about." They drive
relevance scoring (Batch 3) and alert routing (Batch 5). In Batch 1
they're metadata only; nothing reads ``keywords`` yet.

Source-agnostic: a topic is just (slug, label, priority, keywords) +
an optional project association. Aaron creates topics like "AI
agents," "macro markets," or "browser automation" — no marketing-
specific schema.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.db import connect
from core.intelligence.models import Priority, Topic
from core.logging import get_logger

log = get_logger("pilkd.intelligence.topics")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VALID_PRIORITIES = {"low", "medium", "high", "critical"}


class TopicValidationError(ValueError):
    """Raised when an operator submits an invalid topic definition."""


class TopicRegistry:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # ── reads ────────────────────────────────────────────────────

    async def list_topics(
        self, *, project_slug: str | None = None,
    ) -> list[Topic]:
        sql = "SELECT * FROM intel_topics"
        params: list[Any] = []
        if project_slug is not None:
            sql += " WHERE project_slug = ?"
            params.append(project_slug)
        sql += " ORDER BY priority DESC, created_at ASC"
        async with connect(self.db_path) as conn:
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [self._row_to_topic(r) for r in rows]

    async def get(self, topic_id: str) -> Topic | None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM intel_topics WHERE id = ?", (topic_id,),
            ) as cur:
                row = await cur.fetchone()
        return self._row_to_topic(row) if row else None

    async def get_by_slug(self, slug: str) -> Topic | None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM intel_topics WHERE slug = ?", (slug,),
            ) as cur:
                row = await cur.fetchone()
        return self._row_to_topic(row) if row else None

    # ── writes ───────────────────────────────────────────────────

    async def create(
        self,
        *,
        slug: str,
        label: str,
        description: str = "",
        priority: Priority = "medium",
        project_slug: str | None = None,
        keywords: list[str] | None = None,
    ) -> Topic:
        slug = self._validate_slug(slug)
        label_v = self._validate_label(label)
        priority_v = self._validate_priority(priority)
        keywords_v = self._validate_keywords(keywords or [])
        existing = await self.get_by_slug(slug)
        if existing is not None:
            raise TopicValidationError(
                f"a topic with slug '{slug}' already exists"
            )
        new_id = "topic_" + secrets.token_hex(8)
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            await conn.execute(
                """INSERT INTO intel_topics(
                    id, slug, label, description, priority,
                    project_slug, keywords_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id, slug, label_v, description.strip()[:2000],
                    priority_v, project_slug,
                    json.dumps(keywords_v, separators=(",", ":")),
                    now, now,
                ),
            )
            await conn.commit()
        log.info("intel_topic_created", topic_id=new_id, slug=slug)
        topic = await self.get(new_id)
        assert topic is not None
        return topic

    async def update(
        self,
        topic_id: str,
        *,
        label: str | None = None,
        description: str | None = None,
        priority: Priority | None = None,
        keywords: list[str] | None = None,
    ) -> Topic | None:
        existing = await self.get(topic_id)
        if existing is None:
            return None
        sets: list[str] = []
        params: list[Any] = []
        if label is not None:
            sets.append("label = ?")
            params.append(self._validate_label(label))
        if description is not None:
            sets.append("description = ?")
            params.append(description.strip()[:2000])
        if priority is not None:
            sets.append("priority = ?")
            params.append(self._validate_priority(priority))
        if keywords is not None:
            sets.append("keywords_json = ?")
            params.append(
                json.dumps(
                    self._validate_keywords(keywords), separators=(",", ":"),
                )
            )
        if not sets:
            return existing
        sets.append("updated_at = ?")
        params.append(datetime.now(UTC).isoformat())
        params.append(topic_id)
        async with connect(self.db_path) as conn:
            await conn.execute(
                f"UPDATE intel_topics SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            await conn.commit()
        log.info("intel_topic_updated", topic_id=topic_id)
        return await self.get(topic_id)

    async def delete(self, topic_id: str) -> bool:
        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "DELETE FROM intel_topics WHERE id = ?", (topic_id,),
            )
            await conn.commit()
            removed = cur.rowcount or 0
        if removed:
            log.info("intel_topic_deleted", topic_id=topic_id)
        return removed > 0

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _validate_slug(slug: str) -> str:
        s = (slug or "").strip().lower()
        if not _SLUG_RE.match(s):
            raise TopicValidationError(
                f"invalid topic slug '{slug}' — use lowercase letters, "
                "digits, and hyphens (1-64 chars)"
            )
        return s

    @staticmethod
    def _validate_label(label: str) -> str:
        s = (label or "").strip()
        if not s:
            raise TopicValidationError("topic label is required")
        if len(s) > 200:
            raise TopicValidationError("topic label cannot exceed 200 chars")
        return s

    @staticmethod
    def _validate_priority(priority: str) -> str:
        if priority not in _VALID_PRIORITIES:
            raise TopicValidationError(
                f"invalid priority '{priority}'. Valid: "
                f"{sorted(_VALID_PRIORITIES)}"
            )
        return priority

    @staticmethod
    def _validate_keywords(keywords: list[str]) -> list[str]:
        if not isinstance(keywords, list):
            raise TopicValidationError("keywords must be a list of strings")
        cleaned: list[str] = []
        seen: set[str] = set()
        for kw in keywords:
            if not isinstance(kw, str):
                raise TopicValidationError(
                    "every keyword must be a string"
                )
            s = kw.strip()
            if not s:
                continue
            if len(s) > 200:
                raise TopicValidationError(
                    f"keyword too long ({len(s)} chars, max 200)"
                )
            low = s.lower()
            if low in seen:
                continue
            seen.add(low)
            cleaned.append(s)
        if len(cleaned) > 200:
            raise TopicValidationError("at most 200 keywords per topic")
        return cleaned

    @staticmethod
    def _row_to_topic(row: Any) -> Topic:
        try:
            keywords = json.loads(row["keywords_json"] or "[]")
            if not isinstance(keywords, list):
                keywords = []
        except json.JSONDecodeError:
            keywords = []
        return Topic(
            id=row["id"],
            slug=row["slug"],
            label=row["label"],
            description=row["description"] or "",
            priority=row["priority"],
            project_slug=row["project_slug"],
            keywords=keywords,
            mute_until=row["mute_until"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


__all__ = ["TopicRegistry", "TopicValidationError"]
