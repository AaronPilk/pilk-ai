"""Item store — CRUD over the ``intel_items`` table.

Items are individual fetched things (one feed entry, one HN post, one
arXiv paper). Stored deduplicated: the content_hash column is unique
in spirit (we check before insert), and the canonical_url is also
checked so two aggregators republishing the same article collapse to
one row.

Batch 1 only writes items with status ``new`` (post-fetch) or
``stored`` (deduped + persisted). The scoring + brain-write hooks
land in later batches.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.db import connect
from core.intelligence.dedup import canonical_url, content_hash
from core.intelligence.models import ItemStatus
from core.logging import get_logger

log = get_logger("pilkd.intelligence.items")


@dataclass
class IntelItem:
    """One fetched item. Mutable so the (future) scorer + brain
    writer can hand the same object back through the store."""

    id: str
    source_id: str
    title: str
    url: str
    canonical_url: str
    content_hash: str
    fetched_at: str
    status: ItemStatus = "new"
    external_id: str | None = None
    published_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    score: int | None = None
    score_reason: str | None = None
    score_dimensions: dict[str, Any] = field(default_factory=dict)
    brain_path: str | None = None


@dataclass(frozen=True)
class FetchRunSummary:
    """Outcome of a single source fetch — what got stored, what was
    duplicate, what failed. Returned by the manual-refresh endpoint."""

    run_id: str
    source_id: str
    started_at: str
    finished_at: str
    status: str
    items_seen: int
    items_new: int
    items_dup: int
    error: str | None
    new_item_ids: list[str]


@dataclass(frozen=True)
class DigestEntry:
    """One row of the operator-pulled digest. Joins ``intel_items``
    with the source row so the consumer (HTTP route, future
    Reporting agent) gets all the fields it needs in one shape."""

    item_id: str
    title: str
    url: str
    source_slug: str
    source_label: str
    source_kind: str
    project_slug: str | None
    published_at: str | None
    fetched_at: str
    score: int | None
    score_reason: str | None
    brain_path: str | None
    status: str
    matched_topics: list[str]


class ItemStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # ── reads ────────────────────────────────────────────────────

    async def list_items(
        self,
        *,
        source_id: str | None = None,
        status: ItemStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IntelItem]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        sql = "SELECT * FROM intel_items"
        clauses: list[str] = []
        params: list[Any] = []
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY fetched_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with connect(self.db_path) as conn:
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [self._row_to_item(r) for r in rows]

    async def digest(
        self,
        *,
        since: str | None = None,
        project: str | None = None,
        include_global: bool = False,
        source_slug: str | None = None,
        topic: str | None = None,
        min_score: int | None = None,
        limit: int = 50,
    ) -> list[DigestEntry]:
        """Operator-pulled summary of recent items.

        Joins ``intel_items`` with ``intel_sources`` and applies any
        supplied filters. Read-only: no writes, no scoring, no LLM
        calls. Designed to be cheap enough to call on every Reporting
        run without a cost ledger entry.

        Args:
          since: ISO 8601 timestamp (or date prefix). Only items with
            ``fetched_at >= since`` are returned. Lex-comparable strings
            so a partial date like ``2026-04-27`` works.
          project: Filter to sources with ``project_slug = project``.
          include_global: If True AND ``project`` is set, also include
            items from sources with no ``project_slug``. Default False
            so a project-scoped pull doesn't accidentally surface
            global noise.
          source_slug: Filter to one source by its slug.
          topic: Filter to items whose ``score_dimensions_json``
            contains this topic slug as a key. Substring match — we
            avoid the JSON1 SQLite extension dependency.
          min_score: Filter to items with ``score >= min_score``.
          limit: Cap on rows returned (1-200, default 50).
        """
        capped_limit = max(1, min(int(limit), 200))
        sql = (
            "SELECT i.*, "
            "       s.slug AS s_slug, "
            "       s.label AS s_label, "
            "       s.kind AS s_kind, "
            "       s.project_slug AS s_project_slug "
            "  FROM intel_items i "
            "  JOIN intel_sources s ON s.id = i.source_id"
        )
        clauses: list[str] = []
        params: list[Any] = []

        if since:
            clauses.append("i.fetched_at >= ?")
            params.append(since)
        if project:
            if include_global:
                clauses.append(
                    "(s.project_slug = ? OR s.project_slug IS NULL)"
                )
                params.append(project)
            else:
                clauses.append("s.project_slug = ?")
                params.append(project)
        if source_slug:
            clauses.append("s.slug = ?")
            params.append(source_slug)
        if topic:
            # Match items where score_dimensions_json includes the
            # topic slug as a key. The stored JSON looks like
            # {"ai-agents":35,"openai":18}; LIKE on the key pattern
            # is reliable + indexable enough for this scale.
            clauses.append("i.score_dimensions_json LIKE ?")
            params.append(f'%"{topic}":%')
        if min_score is not None:
            try:
                ms = int(min_score)
            except (TypeError, ValueError):
                ms = 0
            clauses.append("i.score IS NOT NULL AND i.score >= ?")
            params.append(max(0, min(100, ms)))

        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY i.fetched_at DESC LIMIT ?"
        params.append(capped_limit)

        async with connect(self.db_path) as conn:
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()

        out: list[DigestEntry] = []
        for r in rows:
            try:
                dims = json.loads(r["score_dimensions_json"] or "{}")
                topics = list(dims.keys()) if isinstance(dims, dict) else []
            except (json.JSONDecodeError, TypeError):
                topics = []
            out.append(
                DigestEntry(
                    item_id=r["id"],
                    title=r["title"],
                    url=r["url"],
                    source_slug=r["s_slug"],
                    source_label=r["s_label"],
                    source_kind=r["s_kind"],
                    project_slug=r["s_project_slug"],
                    published_at=r["published_at"],
                    fetched_at=r["fetched_at"],
                    score=r["score"],
                    score_reason=r["score_reason"],
                    brain_path=r["brain_path"],
                    status=r["status"],
                    matched_topics=topics,
                )
            )
        return out

    async def find_by_hash(self, hash_: str) -> IntelItem | None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM intel_items WHERE content_hash = ? LIMIT 1",
                (hash_,),
            ) as cur:
                row = await cur.fetchone()
        return self._row_to_item(row) if row else None

    async def find_by_canonical_url(self, cu: str) -> IntelItem | None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM intel_items WHERE canonical_url = ? LIMIT 1",
                (cu,),
            ) as cur:
                row = await cur.fetchone()
        return self._row_to_item(row) if row else None

    # ── writes ───────────────────────────────────────────────────

    async def upsert_fetched(
        self,
        *,
        source_id: str,
        title: str,
        url: str,
        body: str = "",
        external_id: str | None = None,
        published_at: str | None = None,
        raw: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> tuple[IntelItem, bool]:
        """Insert a fetched item if it's new. Returns (item, is_new).
        Dedup logic: hash matches OR canonical-url matches → return
        the existing row, do not overwrite. This protects already-
        scored / already-alerted items from regressing if a feed
        republishes them.
        """
        try:
            cu = canonical_url(url)
        except ValueError:
            cu = url.strip()
        h = content_hash(title=title, url=url, body=body)
        existing = await self.find_by_hash(h)
        if existing is None:
            existing = await self.find_by_canonical_url(cu)
        if existing is not None:
            return existing, False

        new_id = "item_" + secrets.token_hex(10)
        now = datetime.now(UTC).isoformat()
        raw_json = json.dumps(raw or {}, separators=(",", ":"))[:200_000]
        async with connect(self.db_path) as conn:
            await conn.execute(
                """INSERT INTO intel_items(
                    id, source_id, external_id, title, url,
                    canonical_url, published_at, fetched_at,
                    content_hash, raw_json, summary, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')""",
                (
                    new_id, source_id, external_id, title.strip()[:500],
                    url, cu, published_at, now, h, raw_json,
                    summary,
                ),
            )
            await conn.commit()
        log.info(
            "intel_item_stored",
            source_id=source_id,
            item_id=new_id,
            title=title[:80],
        )
        item = IntelItem(
            id=new_id,
            source_id=source_id,
            title=title.strip()[:500],
            url=url,
            canonical_url=cu,
            content_hash=h,
            fetched_at=now,
            status="new",
            external_id=external_id,
            published_at=published_at,
            raw=raw or {},
            summary=summary,
        )
        return item, True

    # ── fetch runs (audit log) ───────────────────────────────────

    async def start_run(self, source_id: str) -> str:
        run_id = "run_" + secrets.token_hex(8)
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            await conn.execute(
                """INSERT INTO intel_fetch_runs(
                    id, source_id, started_at, status
                ) VALUES (?, ?, ?, 'running')""",
                (run_id, source_id, now),
            )
            await conn.commit()
        return run_id

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        items_seen: int = 0,
        items_new: int = 0,
        items_dup: int = 0,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            await conn.execute(
                """UPDATE intel_fetch_runs
                       SET finished_at = ?,
                           status = ?,
                           items_seen = ?,
                           items_new = ?,
                           items_dup = ?,
                           error = ?
                     WHERE id = ?""",
                (now, status, items_seen, items_new, items_dup, error, run_id),
            )
            await conn.commit()

    async def get_run(self, run_id: str) -> FetchRunSummary | None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM intel_fetch_runs WHERE id = ?", (run_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return FetchRunSummary(
            run_id=row["id"],
            source_id=row["source_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"] or "",
            status=row["status"],
            items_seen=row["items_seen"] or 0,
            items_new=row["items_new"] or 0,
            items_dup=row["items_dup"] or 0,
            error=row["error"],
            new_item_ids=[],
        )

    async def recent_runs(
        self, *, source_id: str | None = None, limit: int = 20,
    ) -> list[FetchRunSummary]:
        limit = max(1, min(int(limit), 200))
        sql = "SELECT * FROM intel_fetch_runs"
        params: list[Any] = []
        if source_id is not None:
            sql += " WHERE source_id = ?"
            params.append(source_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        async with connect(self.db_path) as conn:
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [
            FetchRunSummary(
                run_id=r["id"],
                source_id=r["source_id"],
                started_at=r["started_at"],
                finished_at=r["finished_at"] or "",
                status=r["status"],
                items_seen=r["items_seen"] or 0,
                items_new=r["items_new"] or 0,
                items_dup=r["items_dup"] or 0,
                error=r["error"],
                new_item_ids=[],
            )
            for r in rows
        ]

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _row_to_item(row: Any) -> IntelItem:
        raw = {}
        try:
            raw = json.loads(row["raw_json"] or "{}")
            if not isinstance(raw, dict):
                raw = {}
        except json.JSONDecodeError:
            raw = {}
        score_dims = {}
        try:
            score_dims = json.loads(row["score_dimensions_json"] or "{}")
            if not isinstance(score_dims, dict):
                score_dims = {}
        except (json.JSONDecodeError, TypeError):
            score_dims = {}
        return IntelItem(
            id=row["id"],
            source_id=row["source_id"],
            title=row["title"],
            url=row["url"],
            canonical_url=row["canonical_url"] or row["url"],
            content_hash=row["content_hash"],
            fetched_at=row["fetched_at"],
            status=row["status"],
            external_id=row["external_id"],
            published_at=row["published_at"],
            raw=raw,
            summary=row["summary"],
            score=row["score"],
            score_reason=row["score_reason"],
            score_dimensions=score_dims,
            brain_path=row["brain_path"],
        )


__all__ = ["DigestEntry", "FetchRunSummary", "IntelItem", "ItemStore"]
