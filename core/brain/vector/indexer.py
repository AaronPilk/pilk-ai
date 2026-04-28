"""Indexer — walks the markdown vault and (re)builds the vector index.

Behavior:
- READ-ONLY against the markdown vault. Never writes a markdown
  file, never deletes one, never moves one.
- Incremental by default: a note is re-embedded only if its
  ``mtime`` differs from the last index OR its content hash has
  changed. A pure mtime touch with identical content is a no-op.
- Source-type derived from the first path segment under the brain
  root: ``persona``, ``projects``, ``world``, ``standing-instructions``,
  ``ingested``. Anything else falls into ``other``.
- ``project_slug`` derived from ``projects/<slug>/...`` paths and
  from ``projects/<slug>/world/...`` paths so a project-scoped
  world note carries the project slug too.
- Cost logging: each ``index_all`` call records one ``cost_entries``
  row per embedding batch with a sensible kind/model breakdown.

The indexer doesn't enable any daemon. The operator triggers it
explicitly (a tool call, an API route, or — in a later batch — a
file-watch event from Phase 4). Auto-indexing is opt-in only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from core.brain.vector.chunker import Chunk, Chunker
from core.brain.vector.embedder import Embedder
from core.brain.vector.store import VectorStore
from core.ledger import Ledger
from core.logging import get_logger

log = get_logger("pilkd.brain.vector.indexer")


# Per-call ceiling. The walk is incremental, so a normal run only
# touches a handful of files; this cap is a safety net for the
# first full index of a fresh vault. The operator can split a
# bigger reindex by calling ``index_all`` repeatedly.
_DEFAULT_MAX_FILES = 5000

# How many chunks to embed in one OpenAI request. The endpoint
# accepts up to 2048 inputs per call but we keep it modest so a
# single failure doesn't redo a large window.
_EMBED_BATCH = 64


@dataclass
class IndexResult:
    """Summary returned to the caller / logged."""

    files_seen: int
    files_changed: int
    files_skipped: int
    files_failed: int
    chunks_indexed: int
    embedding_tokens: int
    estimated_cost_usd: float
    deleted_paths: list[str]


def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _classify(brain_root: Path, file_path: Path) -> tuple[str, str | None]:
    """Return ``(source_type, project_slug)``.

    ``source_type`` is the first directory under the brain root.
    ``project_slug`` is set for files under ``projects/<slug>/...``.
    """
    rel = file_path.relative_to(brain_root)
    parts = rel.parts
    if not parts:
        return ("other", None)
    head = parts[0]
    if head == "projects" and len(parts) >= 2:
        return ("project", parts[1])
    known = {
        "persona",
        "world",
        "standing-instructions",
        "ingested",
        "daily",
        "inbox",
        "sessions",
        "chats",
        "trading",
        "product",
    }
    if head in known:
        return (head, None)
    return ("other", None)


class Indexer:
    """Build / refresh the vector index from a markdown vault."""

    def __init__(
        self,
        *,
        brain_root: Path,
        embedder: Embedder,
        store: VectorStore,
        ledger: Ledger | None = None,
        chunker: Chunker | None = None,
    ) -> None:
        self.brain_root = Path(brain_root)
        self._embedder = embedder
        self._store = store
        self._ledger = ledger
        self._chunker = chunker or Chunker()

    async def index_all(
        self, *, max_files: int = _DEFAULT_MAX_FILES,
        force: bool = False,
    ) -> IndexResult:
        """Walk the vault and index every changed note.

        ``force=True`` re-embeds every file regardless of mtime/hash
        — useful after switching embedding model. ``force=False`` is
        the normal path."""
        if not self.brain_root.exists():
            log.warning(
                "brain_root_missing", path=str(self.brain_root),
            )
            return IndexResult(
                files_seen=0, files_changed=0, files_skipped=0,
                files_failed=0, chunks_indexed=0, embedding_tokens=0,
                estimated_cost_usd=0.0, deleted_paths=[],
            )
        indexed_paths = await self._store.get_indexed_paths()
        seen: set[str] = set()
        files_seen = 0
        files_changed = 0
        files_skipped = 0
        files_failed = 0
        total_chunks = 0
        total_tokens = 0
        total_cost_usd = 0.0
        # Buffer chunk rows + texts and flush in batches of
        # ``_EMBED_BATCH`` to amortize HTTP round-trips. We keep the
        # buffer per run, not per file, so a big run with many
        # tiny notes still hits the embeddings endpoint efficiently.
        pending_rows: list[dict] = []
        pending_texts: list[str] = []

        async def flush() -> None:
            nonlocal total_chunks, total_tokens, total_cost_usd
            if not pending_rows:
                return
            try:
                vectors = await self._embedder.embed(pending_texts)
            except Exception as e:  # pragma: no cover — defensive
                log.warning(
                    "indexer_embed_batch_failed",
                    count=len(pending_rows),
                    error=str(e),
                )
                pending_rows.clear()
                pending_texts.clear()
                raise
            await self._store.upsert_chunks(
                rows=pending_rows, embeddings=vectors,
            )
            batch_chunks = len(pending_rows)
            # Token accounting: rough char-based estimate; the real
            # number from OpenAI is logged inside the embedder.
            batch_tokens = sum(
                max(1, len(t) // 4) for t in pending_texts
            )
            batch_cost = 0.0
            if hasattr(self._embedder, "estimated_cost_usd"):
                batch_cost = float(
                    self._embedder.estimated_cost_usd(batch_tokens)
                )
            total_chunks += batch_chunks
            total_tokens += batch_tokens
            total_cost_usd += batch_cost
            if self._ledger is not None and batch_cost > 0:
                try:
                    await self._ledger.record_embedding(
                        model=self._embedder.model,
                        input_tokens=batch_tokens,
                        usd=batch_cost,
                        agent_name="brain_indexer",
                        metadata={
                            "chunks": batch_chunks,
                            "embedder_dim": self._embedder.dim,
                            "source": "brain.vector.indexer",
                        },
                    )
                except Exception as e:  # pragma: no cover
                    log.warning("ledger_embed_record_failed", error=str(e))
            pending_rows.clear()
            pending_texts.clear()

        # Walk the vault deterministically (sorted for stable
        # behavior across runs).
        for file_path in sorted(self.brain_root.rglob("*.md")):
            if files_seen >= max_files:
                log.info(
                    "indexer_max_files_reached", max_files=max_files,
                )
                break
            files_seen += 1
            try:
                rel = str(file_path.relative_to(self.brain_root))
                seen.add(rel)
                stat = file_path.stat()
                if not force and rel in indexed_paths:
                    prev_mtime, prev_hash = indexed_paths[rel]
                    # Quick mtime gate first to avoid hashing every
                    # file on every walk.
                    if abs(stat.st_mtime - prev_mtime) < 0.001:
                        files_skipped += 1
                        continue
                    text = file_path.read_text(encoding="utf-8")
                    new_hash = _hash_content(text)
                    if new_hash == prev_hash:
                        files_skipped += 1
                        continue
                else:
                    text = file_path.read_text(encoding="utf-8")
                    new_hash = _hash_content(text)
                source_type, project_slug = _classify(
                    self.brain_root, file_path,
                )
                # Drop any prior chunks for this path so a shorter
                # rewrite doesn't leave stale tail chunks in the
                # index.
                await self._store.delete_by_path(rel)
                chunks = self._chunker.chunk(text)
                if not chunks:
                    # File too small or empty after chunking — that's
                    # fine; we already cleared old chunks above.
                    files_changed += 1
                    continue
                indexed_at = datetime.now(UTC).isoformat()
                for ch in chunks:
                    chunk_id = (
                        f"chk_{hashlib.sha256(f'{rel}:{ch.idx}'.encode()).hexdigest()[:16]}"
                    )
                    pending_rows.append(
                        {
                            "id": chunk_id,
                            "brain_path": rel,
                            "chunk_idx": ch.idx,
                            "heading": ch.heading,
                            "content": ch.content,
                            "project_slug": project_slug,
                            "source_type": source_type,
                            "file_mtime": stat.st_mtime,
                            "file_hash": new_hash,
                            "indexed_at": indexed_at,
                            "embedding_model": self._embedder.model,
                        }
                    )
                    pending_texts.append(
                        # Embed heading + content so semantic
                        # queries that reference section names hit.
                        f"{ch.heading}\n\n{ch.content}"
                        if ch.heading
                        else ch.content
                    )
                    if len(pending_rows) >= _EMBED_BATCH:
                        await flush()
                files_changed += 1
            except Exception as e:  # noqa: BLE001
                files_failed += 1
                log.warning(
                    "indexer_file_failed",
                    file=str(file_path),
                    error=str(e),
                )
                continue
        await flush()
        # Drop chunks for any note that disappeared from disk since
        # the last walk. We ONLY do this when a path was previously
        # indexed AND isn't seen this walk — never reach into the
        # markdown vault to confirm.
        deleted: list[str] = []
        for prev_path in indexed_paths:
            if prev_path not in seen:
                await self._store.delete_by_path(prev_path)
                deleted.append(prev_path)
        if deleted:
            log.info(
                "indexer_dropped_stale_paths",
                count=len(deleted),
            )
        return IndexResult(
            files_seen=files_seen,
            files_changed=files_changed,
            files_skipped=files_skipped,
            files_failed=files_failed,
            chunks_indexed=total_chunks,
            embedding_tokens=total_tokens,
            estimated_cost_usd=total_cost_usd,
            deleted_paths=deleted,
        )


__all__ = ["IndexResult", "Indexer"]
