"""Persistence layer for ``ingested_files``.

Append-style: every operator drop creates one row. Status moves
through ``pending → extracting → indexing → done`` (or → ``failed``
with the error). The hash unique-index gives us free dedup at
insert time.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.db import connect


def _uid() -> str:
    return f"ing_{secrets.token_hex(8)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class IngestRow:
    id: str
    original_path: str
    stored_path: str | None
    file_type: str
    project_slug: str | None
    content_hash: str
    byte_size: int
    status: str
    extracted_text_path: str | None = None
    brain_note_path: str | None = None
    summary: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class IngestRegistry:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    async def register(
        self,
        *,
        original_path: str,
        file_type: str,
        content_hash: str,
        byte_size: int,
        stored_path: str | None = None,
        project_slug: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[IngestRow, bool]:
        """Insert a new row OR return the existing row when the hash
        already exists. Bool flag indicates whether it was newly
        inserted (``True``) or a duplicate (``False``)."""
        existing = await self.get_by_hash(content_hash)
        if existing is not None:
            return existing, False
        rid = _uid()
        now = _now()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO ingested_files("
                "id, original_path, stored_path, file_type, "
                "project_slug, content_hash, byte_size, status, "
                "metadata_json, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    rid, original_path, stored_path, file_type,
                    project_slug, content_hash, int(byte_size),
                    json.dumps(metadata or {}), now, now,
                ),
            )
            await conn.commit()
        row = await self.get(rid)
        assert row is not None
        return row, True

    async def get(self, rid: str) -> IngestRow | None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT id, original_path, stored_path, file_type, "
                "project_slug, content_hash, byte_size, status, "
                "extracted_text_path, brain_note_path, summary, "
                "error, metadata_json, created_at, updated_at "
                "FROM ingested_files WHERE id = ?",
                (rid,),
            ) as cur:
                row = await cur.fetchone()
        return _hydrate(row) if row else None

    async def get_by_hash(self, content_hash: str) -> IngestRow | None:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT id, original_path, stored_path, file_type, "
                "project_slug, content_hash, byte_size, status, "
                "extracted_text_path, brain_note_path, summary, "
                "error, metadata_json, created_at, updated_at "
                "FROM ingested_files WHERE content_hash = ?",
                (content_hash,),
            ) as cur:
                row = await cur.fetchone()
        return _hydrate(row) if row else None

    async def update(
        self,
        rid: str,
        *,
        status: str | None = None,
        stored_path: str | None = None,
        extracted_text_path: str | None = None,
        brain_note_path: str | None = None,
        summary: str | None = None,
        error: str | None = None,
        metadata_patch: dict[str, Any] | None = None,
    ) -> IngestRow:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if stored_path is not None:
            sets.append("stored_path = ?")
            params.append(stored_path)
        if extracted_text_path is not None:
            sets.append("extracted_text_path = ?")
            params.append(extracted_text_path)
        if brain_note_path is not None:
            sets.append("brain_note_path = ?")
            params.append(brain_note_path)
        if summary is not None:
            sets.append("summary = ?")
            params.append(summary)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if metadata_patch is not None:
            row = await self.get(rid)
            existing = (row.metadata if row else {}) or {}
            existing.update(metadata_patch)
            sets.append("metadata_json = ?")
            params.append(json.dumps(existing))
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(rid)
        sql = (
            "UPDATE ingested_files SET "
            + ", ".join(sets)
            + " WHERE id = ?"
        )
        async with connect(self.db_path) as conn:
            await conn.execute(sql, params)
            await conn.commit()
        row = await self.get(rid)
        assert row is not None
        return row

    async def list_recent(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
    ) -> list[IngestRow]:
        wheres: list[str] = []
        params: list[Any] = []
        if status is not None:
            wheres.append("status = ?")
            params.append(status)
        where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(int(limit))
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT id, original_path, stored_path, file_type, "
                "project_slug, content_hash, byte_size, status, "
                "extracted_text_path, brain_note_path, summary, "
                "error, metadata_json, created_at, updated_at "
                "FROM ingested_files" + where_sql
                + " ORDER BY created_at DESC LIMIT ?",
                params,
            ) as cur:
                rows = await cur.fetchall()
        return [_hydrate(r) for r in rows]


def _hydrate(row: Any) -> IngestRow:
    meta_raw = row[12] or "{}"
    try:
        meta = json.loads(meta_raw)
    except json.JSONDecodeError:
        meta = {}
    return IngestRow(
        id=row[0], original_path=row[1], stored_path=row[2],
        file_type=row[3], project_slug=row[4], content_hash=row[5],
        byte_size=int(row[6]), status=row[7],
        extracted_text_path=row[8], brain_note_path=row[9],
        summary=row[10], error=row[11], metadata=meta,
        created_at=row[13] or "", updated_at=row[14] or "",
    )


__all__ = ["IngestRegistry", "IngestRow"]
