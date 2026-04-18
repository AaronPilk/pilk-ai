"""SQLite-backed store for structured memory entries.

One table, one class, four query shapes (list / list-by-kind /
delete-one / clear). The entry shape is intentionally flat so the UI
can render it without joins.
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from core.db import connect


class MemoryKind(StrEnum):
    PREFERENCE = "preference"
    STANDING_INSTRUCTION = "standing_instruction"
    FACT = "fact"
    PATTERN = "pattern"


VALID_KINDS: frozenset[str] = frozenset(k.value for k in MemoryKind)


@dataclass
class MemoryEntry:
    id: str
    kind: str
    title: str
    body: str
    source: str           # "user" for manual adds; future: "assistant" | agent name
    plan_id: str | None
    created_at: str
    updated_at: str

    def public_dict(self) -> dict:
        return asdict(self)


def _new_id() -> str:
    return f"mem_{secrets.token_hex(6)}"


class MemoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def add(
        self,
        *,
        kind: str,
        title: str,
        body: str,
        source: str = "user",
        plan_id: str | None = None,
    ) -> MemoryEntry:
        if kind not in VALID_KINDS:
            raise ValueError(f"unknown memory kind: {kind}")
        title = title.strip()
        body = body.strip()
        if not title:
            raise ValueError("memory entry requires a title")
        now = datetime.now(UTC).isoformat()
        entry = MemoryEntry(
            id=_new_id(),
            kind=kind,
            title=title,
            body=body,
            source=source,
            plan_id=plan_id,
            created_at=now,
            updated_at=now,
        )
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO memory_entries("
                "id, kind, title, body, source, plan_id, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.id,
                    entry.kind,
                    entry.title,
                    entry.body,
                    entry.source,
                    entry.plan_id,
                    entry.created_at,
                    entry.updated_at,
                ),
            )
            await conn.commit()
        return entry

    async def list(self, kind: str | None = None) -> list[MemoryEntry]:
        sql = (
            "SELECT id, kind, title, body, source, plan_id, created_at, updated_at "
            "FROM memory_entries"
        )
        args: tuple = ()
        if kind is not None:
            if kind not in VALID_KINDS:
                raise ValueError(f"unknown memory kind: {kind}")
            sql += " WHERE kind = ?"
            args = (kind,)
        # rowid tiebreaks collisions when inserts happen in the same tick.
        sql += " ORDER BY datetime(created_at) DESC, rowid DESC"
        async with connect(self.db_path) as conn, conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [MemoryEntry(**dict(r)) for r in rows]

    async def delete(self, entry_id: str) -> bool:
        async with connect(self.db_path) as conn:
            cur = await conn.execute(
                "DELETE FROM memory_entries WHERE id = ?", (entry_id,)
            )
            await conn.commit()
            return (cur.rowcount or 0) > 0

    async def clear(self, kind: str | None = None) -> int:
        sql = "DELETE FROM memory_entries"
        args: tuple = ()
        if kind is not None:
            if kind not in VALID_KINDS:
                raise ValueError(f"unknown memory kind: {kind}")
            sql += " WHERE kind = ?"
            args = (kind,)
        async with connect(self.db_path) as conn:
            cur = await conn.execute(sql, args)
            await conn.commit()
            return cur.rowcount or 0
