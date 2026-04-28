"""Vector store interface + local SQLite implementation.

Schema lives in migrations v10. We keep chunk metadata and the raw
embedding bytes in two tables so a future swap to ``sqlite-vec``
(or any other ANN backend) only touches the embedding storage —
the chunk metadata stays canonical.

Cosine similarity is computed in Python with numpy. For PILK's
scale (low five-figures of chunks) this is fast enough that the
extra complexity of a vector index isn't worth it. If/when that
changes the ``VectorStore`` interface keeps the call sites stable.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

import numpy as np

from core.db import connect
from core.logging import get_logger

log = get_logger("pilkd.brain.vector.store")


@dataclass
class StoredChunk:
    """Read-back shape from the store. ``score`` is set by search,
    not by ingest, so it carries ``None`` until the search pass
    computes cosine similarity."""

    chunk_id: str
    brain_path: str
    chunk_idx: int
    heading: str | None
    content: str
    project_slug: str | None
    source_type: str
    indexed_at: str
    embedding_model: str
    score: float | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class VectorStore(Protocol):
    async def upsert_chunks(
        self,
        *,
        rows: list[dict],
        embeddings: list[list[float]],
    ) -> None:
        """Insert or replace chunks + their vectors. ``rows`` and
        ``embeddings`` must have the same length. Each row carries:
        ``id`` (chunk_id), ``brain_path``, ``chunk_idx``, ``heading``,
        ``content``, ``project_slug``, ``source_type``, ``file_mtime``,
        ``file_hash``, ``indexed_at``, ``embedding_model``."""
        ...

    async def delete_by_path(self, brain_path: str) -> int:
        """Drop all chunks for a single note. Returns rows deleted.
        Used when a file is removed or re-indexed clean."""
        ...

    async def get_indexed_paths(self) -> dict[str, tuple[float, str]]:
        """Return ``{brain_path: (file_mtime, file_hash)}`` for every
        already-indexed note. Indexer uses this to skip unchanged
        files in the incremental walk."""
        ...

    async def search(
        self,
        *,
        query_embedding: list[float],
        limit: int = 10,
        project_slug: str | None = None,
        source_type: str | None = None,
        min_score: float | None = None,
    ) -> list[StoredChunk]:
        """Top-``limit`` chunks by cosine similarity. Optional
        filters narrow the candidate pool BEFORE ranking — passing
        ``project_slug='skyway-sales'`` hides global notes; passing
        ``source_type='world'`` restricts to intelligence notes."""
        ...

    async def stats(self) -> dict:
        """Quick counts for ops dashboards. Returns a small dict
        with ``chunk_count``, ``note_count``, ``by_source`` map."""
        ...


# ── Local SQLite implementation ────────────────────────────────────


def _pack(vec: list[float]) -> bytes:
    """Pack a vector as a little-endian float32 BLOB. We store dim
    in the row so unpacking knows how many floats to read."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4", count=dim)


class LocalSQLiteVectorStore:
    """Default implementation: chunk rows + packed-float blobs in
    the existing PILK SQLite database.

    Search loads the candidate set into a numpy matrix and computes
    cosine similarity in a single matmul. For PILK's working scale
    this is well under a second on a normal laptop and avoids
    introducing a new dependency.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    async def upsert_chunks(
        self,
        *,
        rows: list[dict],
        embeddings: list[list[float]],
    ) -> None:
        if not rows:
            return
        if len(rows) != len(embeddings):
            raise ValueError(
                f"rows ({len(rows)}) and embeddings ({len(embeddings)}) "
                f"must have the same length"
            )
        async with connect(self.db_path) as conn:
            for r, vec in zip(rows, embeddings, strict=True):
                # Replace any existing chunk at this (brain_path,
                # chunk_idx). The ON CONFLICT works on the UNIQUE
                # index defined in the migration.
                await conn.execute(
                    "INSERT INTO brain_chunks(id, brain_path, chunk_idx, "
                    "heading, content, project_slug, source_type, "
                    "file_mtime, file_hash, indexed_at, embedding_model) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(brain_path, chunk_idx) DO UPDATE SET "
                    "id=excluded.id, heading=excluded.heading, "
                    "content=excluded.content, "
                    "project_slug=excluded.project_slug, "
                    "source_type=excluded.source_type, "
                    "file_mtime=excluded.file_mtime, "
                    "file_hash=excluded.file_hash, "
                    "indexed_at=excluded.indexed_at, "
                    "embedding_model=excluded.embedding_model",
                    (
                        r["id"], r["brain_path"], r["chunk_idx"],
                        r.get("heading"), r["content"],
                        r.get("project_slug"), r["source_type"],
                        float(r["file_mtime"]), r["file_hash"],
                        r["indexed_at"], r["embedding_model"],
                    ),
                )
                await conn.execute(
                    "INSERT INTO brain_embeddings(chunk_id, vector, dim) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(chunk_id) DO UPDATE SET "
                    "vector=excluded.vector, dim=excluded.dim",
                    (r["id"], _pack(vec), len(vec)),
                )
            await conn.commit()

    async def delete_by_path(self, brain_path: str) -> int:
        async with connect(self.db_path) as conn:
            # CASCADE on the FK takes care of brain_embeddings.
            cur = await conn.execute(
                "DELETE FROM brain_chunks WHERE brain_path = ?",
                (brain_path,),
            )
            await conn.commit()
            return cur.rowcount or 0

    async def get_indexed_paths(self) -> dict[str, tuple[float, str]]:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT brain_path, MAX(file_mtime), MAX(file_hash) "
                "FROM brain_chunks GROUP BY brain_path"
            ) as cur:
                rows = await cur.fetchall()
        return {r[0]: (float(r[1] or 0.0), r[2] or "") for r in rows}

    async def search(
        self,
        *,
        query_embedding: list[float],
        limit: int = 10,
        project_slug: str | None = None,
        source_type: str | None = None,
        min_score: float | None = None,
    ) -> list[StoredChunk]:
        # Pull the candidate pool with optional filters. We rank in
        # Python so SQLite stays a pure key/value store.
        wheres: list[str] = []
        params: list = []
        if project_slug is not None:
            wheres.append("c.project_slug = ?")
            params.append(project_slug)
        if source_type is not None:
            wheres.append("c.source_type = ?")
            params.append(source_type)
        where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT c.id, c.brain_path, c.chunk_idx, c.heading, "
                "c.content, c.project_slug, c.source_type, c.indexed_at, "
                "c.embedding_model, e.vector, e.dim "
                "FROM brain_chunks c JOIN brain_embeddings e "
                "ON e.chunk_id = c.id" + where_sql,
                params,
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return []
        q = np.array(query_embedding, dtype=np.float32)
        q_norm = float(np.linalg.norm(q))
        if q_norm == 0.0:
            return []
        q = q / q_norm
        # Build the matrix lazily — chunks may have heterogeneous
        # dims if a re-embed mid-flight changed the model. We only
        # rank chunks that match the query's dim; the rest are
        # silently skipped (they'll get re-indexed on the next walk).
        scored: list[tuple[float, dict]] = []
        for r in rows:
            dim = int(r[10])
            if dim != q.shape[0]:
                continue
            vec = _unpack(r[9], dim).astype(np.float32, copy=False)
            v_norm = float(np.linalg.norm(vec))
            if v_norm == 0.0:
                continue
            score = float(np.dot(q, vec) / v_norm)
            if min_score is not None and score < min_score:
                continue
            scored.append((score, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        out: list[StoredChunk] = []
        for score, r in scored[:limit]:
            out.append(
                StoredChunk(
                    chunk_id=r[0],
                    brain_path=r[1],
                    chunk_idx=int(r[2]),
                    heading=r[3],
                    content=r[4],
                    project_slug=r[5],
                    source_type=r[6],
                    indexed_at=r[7],
                    embedding_model=r[8],
                    score=score,
                )
            )
        return out

    async def stats(self) -> dict:
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT brain_path) "
                "FROM brain_chunks"
            ) as cur:
                row = await cur.fetchone()
                chunk_count = int(row[0] or 0)
                note_count = int(row[1] or 0)
            async with conn.execute(
                "SELECT source_type, COUNT(*) FROM brain_chunks "
                "GROUP BY source_type"
            ) as cur:
                by_source = {
                    str(r[0]): int(r[1]) for r in await cur.fetchall()
                }
        return {
            "chunk_count": chunk_count,
            "note_count": note_count,
            "by_source": by_source,
        }


__all__ = [
    "LocalSQLiteVectorStore",
    "StoredChunk",
    "VectorStore",
]
