"""High-level semantic search facade.

Glues an ``Embedder`` and a ``VectorStore`` together. This is the
surface the brain tool and the API route call. Keeping it thin
means the real moving parts (embedding model, store backend) can
be swapped without changes to callers.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.brain.vector.embedder import Embedder, EmbeddingError
from core.brain.vector.store import StoredChunk, VectorStore
from core.logging import get_logger

log = get_logger("pilkd.brain.vector.search")


@dataclass
class SearchHit:
    """Search-result shape exposed to tools and routes."""

    brain_path: str
    chunk_idx: int
    heading: str | None
    content: str
    project_slug: str | None
    source_type: str
    score: float


class SemanticSearch:
    """Plain composition: embed the query, ask the store for top-k.

    No re-ranking, no MMR, no BM25 hybrid yet — those are easy to
    add later but not worth the complexity for a first cut. Keep
    the pipeline small and observable.
    """

    def __init__(
        self,
        *,
        embedder: Embedder,
        store: VectorStore,
    ) -> None:
        self._embedder = embedder
        self._store = store

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        project_slug: str | None = None,
        source_type: str | None = None,
        min_score: float | None = None,
    ) -> list[SearchHit]:
        q = (query or "").strip()
        if not q:
            return []
        try:
            vectors = await self._embedder.embed([q])
        except EmbeddingError:
            raise
        if not vectors or not vectors[0]:
            log.warning("semantic_search_empty_embedding", query=q[:80])
            return []
        chunks: list[StoredChunk] = await self._store.search(
            query_embedding=vectors[0],
            limit=limit,
            project_slug=project_slug,
            source_type=source_type,
            min_score=min_score,
        )
        hits: list[SearchHit] = []
        for c in chunks:
            hits.append(
                SearchHit(
                    brain_path=c.brain_path,
                    chunk_idx=c.chunk_idx,
                    heading=c.heading,
                    content=c.content,
                    project_slug=c.project_slug,
                    source_type=c.source_type,
                    score=c.score or 0.0,
                )
            )
        return hits


__all__ = ["SearchHit", "SemanticSearch"]
