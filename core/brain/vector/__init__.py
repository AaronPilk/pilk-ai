"""Vector retrieval layer over the markdown brain vault.

Markdown files under ``~/PILK-brain`` remain the source of truth.
This subpackage builds a derived semantic index on top:

  - ``Embedder``      — turns text into a dense vector. Default is
                        OpenAI ``text-embedding-3-small`` over httpx.
  - ``VectorStore``   — stores chunk metadata + embeddings. Local
                        SQLite implementation by default; the
                        interface is small enough that a future
                        ``sqlite-vec`` adapter can drop in without
                        changing call sites.
  - ``Chunker``       — splits markdown into heading-aware chunks.
  - ``Indexer``       — walks the vault and (re)indexes notes whose
                        ``mtime`` or content hash has changed. The
                        indexer is read-only for the markdown side
                        — it never writes to the vault.
  - ``SemanticSearch`` — high-level search that combines embedder +
                        store and ranks by cosine similarity.

Costs are logged through the existing ``Ledger`` so the operator
can see per-day embedding spend in the same dashboard as LLM spend.
"""

from core.brain.vector.chunker import Chunk, Chunker
from core.brain.vector.embedder import (
    Embedder,
    EmbeddingError,
    OpenAIEmbedder,
    make_embedder,
)
from core.brain.vector.search import SearchHit, SemanticSearch
from core.brain.vector.store import (
    LocalSQLiteVectorStore,
    StoredChunk,
    VectorStore,
)

__all__ = [
    "Chunk",
    "Chunker",
    "Embedder",
    "EmbeddingError",
    "LocalSQLiteVectorStore",
    "OpenAIEmbedder",
    "SearchHit",
    "SemanticSearch",
    "StoredChunk",
    "VectorStore",
    "make_embedder",
]
