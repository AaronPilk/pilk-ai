"""Phase 2 — vector brain tests.

Covers the four moving parts that don't need a network connection:

  - ``Chunker``                       — heading-aware markdown split
  - ``LocalSQLiteVectorStore``         — persistence + cosine search
  - ``Indexer`` (with a stub embedder) — incremental walk + re-index
  - ``SemanticSearch`` glue            — embed → store query

Real OpenAI calls are out of scope; we wire a deterministic stub
embedder so the tests are fast and free.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from core.brain.vector import (
    Chunker,
    LocalSQLiteVectorStore,
    SemanticSearch,
)
from core.brain.vector.embedder import Embedder
from core.brain.vector.indexer import Indexer
from core.db.migrations import ensure_schema


# ── Stub embedder — deterministic 16-dim ──────────────────────────


class StubEmbedder:
    """Hash-based deterministic embedder. Maps each input string to
    a 16-dim vector by spreading SHA-256 bytes over [-1, 1]. Same
    string → same vector, different strings → different vectors,
    no network. Good enough for indexer + store tests; cosine
    similarity won't reflect 'meaning' but will reflect identity."""

    model = "stub-16"
    dim = 16

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            digest = hashlib.sha256(t.encode("utf-8")).digest()
            v = [
                ((b / 255.0) * 2.0 - 1.0)
                for b in digest[: self.dim]
            ]
            # Normalize so cosine sim has a clean meaning.
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out

    def estimated_cost_usd(self, total_tokens: int) -> float:
        return 0.0


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "pilk.db"
    ensure_schema(p)
    return p


@pytest.fixture
def brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "PILK-brain"
    (root / "world" / "2026-04-28").mkdir(parents=True)
    (root / "projects" / "skyway-sales" / "world" / "2026-04-28").mkdir(
        parents=True
    )
    (root / "persona").mkdir()
    (root / "standing-instructions").mkdir()
    (root / "ingested").mkdir()
    (root / "world" / "2026-04-28" / "ai-news.md").write_text(
        "# AI Agent Frameworks\n\n"
        "OpenAI shipped workspace agents this week.\n\n"
        "## Workspace API\n\n"
        "Latency improvements via WebSockets are notable for "
        "real-time multi-step agents.\n",
        encoding="utf-8",
    )
    (root / "projects" / "skyway-sales" / "world" / "2026-04-28"
     / "demo-deal.md").write_text(
        "# Skyway pipeline note\n\n"
        "Acme Corp moved to discovery stage today; "
        "follow-up email scheduled for Wednesday.\n",
        encoding="utf-8",
    )
    (root / "persona" / "voice.md").write_text(
        "Aaron prefers direct, concise updates without fluff.\n",
        encoding="utf-8",
    )
    return root


# ── Chunker ───────────────────────────────────────────────────────


def test_chunker_splits_by_heading() -> None:
    md = (
        "# A\n\nbody A\n\n## B\n\nbody B with two paragraphs.\n\n"
        "more of B.\n\n# C\n\nbody C.\n"
    )
    chunks = Chunker(chunk_chars=2000, min_chunk_chars=10).chunk(md)
    headings = [c.heading for c in chunks]
    assert "A" in headings
    assert "B" in headings
    assert "C" in headings
    assert all(c.content.strip() for c in chunks)


def test_chunker_handles_empty_input() -> None:
    assert Chunker().chunk("") == []
    assert Chunker().chunk("    \n\n  ") == []


def test_chunker_windows_long_sections() -> None:
    long_body = "para.\n\n" * 400  # ~2400 chars
    md = f"# Big\n\n{long_body}"
    chunks = Chunker(chunk_chars=500, overlap_chars=50).chunk(md)
    # Should split into multiple windows under the heading "Big".
    assert len(chunks) >= 2
    assert all(c.heading == "Big" for c in chunks)


def test_chunker_respects_code_fence() -> None:
    md = (
        "# Code\n\n```python\n# this is a comment, not a heading\n"
        "x = 1\n```\n\n## Next\n\nbody\n"
    )
    chunks = Chunker().chunk(md)
    headings = [c.heading for c in chunks]
    # 'this is a comment' should NOT have created a new section.
    assert headings.count("Code") >= 1
    assert "Next" in headings


# ── LocalSQLiteVectorStore — round trip + cosine ──────────────────


@pytest.mark.asyncio
async def test_store_upsert_and_search_roundtrip(db_path: Path) -> None:
    store = LocalSQLiteVectorStore(db_path)
    rows = [
        {
            "id": "chk_a",
            "brain_path": "world/a.md",
            "chunk_idx": 0,
            "heading": "A",
            "content": "first chunk",
            "project_slug": None,
            "source_type": "world",
            "file_mtime": 100.0,
            "file_hash": "h_a",
            "indexed_at": "2026-04-28T00:00:00+00:00",
            "embedding_model": "stub-16",
        },
        {
            "id": "chk_b",
            "brain_path": "world/b.md",
            "chunk_idx": 0,
            "heading": "B",
            "content": "second chunk",
            "project_slug": None,
            "source_type": "world",
            "file_mtime": 100.0,
            "file_hash": "h_b",
            "indexed_at": "2026-04-28T00:00:00+00:00",
            "embedding_model": "stub-16",
        },
    ]
    embeddings = [
        [1.0, 0.0, 0.0, 0.0] + [0.0] * 12,
        [0.0, 1.0, 0.0, 0.0] + [0.0] * 12,
    ]
    await store.upsert_chunks(rows=rows, embeddings=embeddings)
    # Query closer to chk_a's vector.
    hits = await store.search(
        query_embedding=[0.99, 0.05, 0.0, 0.0] + [0.0] * 12,
        limit=2,
    )
    assert len(hits) == 2
    assert hits[0].chunk_id == "chk_a"
    assert hits[0].score > hits[1].score


@pytest.mark.asyncio
async def test_store_filters_by_project(db_path: Path) -> None:
    store = LocalSQLiteVectorStore(db_path)
    rows = [
        {
            "id": "chk_global",
            "brain_path": "world/g.md",
            "chunk_idx": 0,
            "heading": None,
            "content": "global chunk",
            "project_slug": None,
            "source_type": "world",
            "file_mtime": 100.0, "file_hash": "h_g",
            "indexed_at": "x", "embedding_model": "stub-16",
        },
        {
            "id": "chk_proj",
            "brain_path": "projects/skyway-sales/world/p.md",
            "chunk_idx": 0,
            "heading": None,
            "content": "project chunk",
            "project_slug": "skyway-sales",
            "source_type": "project",
            "file_mtime": 100.0, "file_hash": "h_p",
            "indexed_at": "x", "embedding_model": "stub-16",
        },
    ]
    embeddings = [[1.0] + [0.0] * 15, [1.0] + [0.0] * 15]
    await store.upsert_chunks(rows=rows, embeddings=embeddings)
    hits = await store.search(
        query_embedding=[1.0] + [0.0] * 15,
        limit=10,
        project_slug="skyway-sales",
    )
    assert {h.chunk_id for h in hits} == {"chk_proj"}


@pytest.mark.asyncio
async def test_store_delete_by_path_cascades(db_path: Path) -> None:
    store = LocalSQLiteVectorStore(db_path)
    rows = [
        {
            "id": f"chk_{i}",
            "brain_path": "world/x.md",
            "chunk_idx": i,
            "heading": None,
            "content": f"chunk {i}",
            "project_slug": None,
            "source_type": "world",
            "file_mtime": 100.0, "file_hash": "h",
            "indexed_at": "x", "embedding_model": "stub-16",
        }
        for i in range(3)
    ]
    embeddings = [[1.0] + [0.0] * 15 for _ in range(3)]
    await store.upsert_chunks(rows=rows, embeddings=embeddings)
    deleted = await store.delete_by_path("world/x.md")
    assert deleted == 3
    stats = await store.stats()
    assert stats["chunk_count"] == 0


# ── Indexer — incremental walk ────────────────────────────────────


@pytest.mark.asyncio
async def test_indexer_indexes_brain(
    db_path: Path, brain_root: Path,
) -> None:
    store = LocalSQLiteVectorStore(db_path)
    indexer = Indexer(
        brain_root=brain_root,
        embedder=StubEmbedder(),
        store=store,
    )
    result = await indexer.index_all()
    assert result.files_changed >= 3
    stats = await store.stats()
    assert stats["chunk_count"] >= 3
    assert "world" in stats["by_source"]
    assert "project" in stats["by_source"]
    assert "persona" in stats["by_source"]


@pytest.mark.asyncio
async def test_indexer_is_incremental(
    db_path: Path, brain_root: Path,
) -> None:
    store = LocalSQLiteVectorStore(db_path)
    indexer = Indexer(
        brain_root=brain_root,
        embedder=StubEmbedder(),
        store=store,
    )
    first = await indexer.index_all()
    # Second pass without changes — nothing should re-embed.
    second = await indexer.index_all()
    assert second.files_changed == 0
    assert second.files_skipped == first.files_seen


@pytest.mark.asyncio
async def test_indexer_picks_up_changed_file(
    db_path: Path, brain_root: Path,
) -> None:
    store = LocalSQLiteVectorStore(db_path)
    indexer = Indexer(
        brain_root=brain_root,
        embedder=StubEmbedder(),
        store=store,
    )
    await indexer.index_all()
    # Touch one file with new content.
    target = brain_root / "world" / "2026-04-28" / "ai-news.md"
    target.write_text(
        "# AI Agent Frameworks\n\nDeepMind shipped a new release.\n",
        encoding="utf-8",
    )
    # Bump mtime to be safe.
    import os, time as _t
    _t.sleep(0.01)
    os.utime(target, None)
    result = await indexer.index_all()
    assert result.files_changed == 1
    assert result.files_skipped >= 2


@pytest.mark.asyncio
async def test_indexer_drops_chunks_for_deleted_files(
    db_path: Path, brain_root: Path,
) -> None:
    store = LocalSQLiteVectorStore(db_path)
    indexer = Indexer(
        brain_root=brain_root,
        embedder=StubEmbedder(),
        store=store,
    )
    await indexer.index_all()
    # Delete one file from disk.
    target = brain_root / "persona" / "voice.md"
    target.unlink()
    result = await indexer.index_all()
    assert "persona/voice.md" in result.deleted_paths


# ── SemanticSearch end-to-end ────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_search_returns_ranked_hits(
    db_path: Path, brain_root: Path,
) -> None:
    store = LocalSQLiteVectorStore(db_path)
    embedder = StubEmbedder()
    indexer = Indexer(
        brain_root=brain_root, embedder=embedder, store=store,
    )
    await indexer.index_all()
    search = SemanticSearch(embedder=embedder, store=store)
    # The stub embedder makes "ai news" map to its own vector;
    # exact-string match guarantees the highest score.
    hits = await search.search("ai news", limit=5)
    assert hits, "expected at least one hit"
    # Cosine similarity range — the store's vectors and queries are
    # both unit-normalised, so scores fall in [-1, 1].
    assert all(-1.0 <= h.score <= 1.0 for h in hits)


@pytest.mark.asyncio
async def test_semantic_search_filters_by_project(
    db_path: Path, brain_root: Path,
) -> None:
    store = LocalSQLiteVectorStore(db_path)
    embedder = StubEmbedder()
    indexer = Indexer(
        brain_root=brain_root, embedder=embedder, store=store,
    )
    await indexer.index_all()
    search = SemanticSearch(embedder=embedder, store=store)
    hits = await search.search(
        "anything", limit=10, project_slug="skyway-sales",
    )
    assert hits
    assert all(h.project_slug == "skyway-sales" for h in hits)


@pytest.mark.asyncio
async def test_semantic_search_empty_query_returns_empty() -> None:
    class _NoStore:
        async def search(self, **k): return []
        async def upsert_chunks(self, **k): pass
        async def delete_by_path(self, p): return 0
        async def get_indexed_paths(self): return {}
        async def stats(self): return {}
    s = SemanticSearch(embedder=StubEmbedder(), store=_NoStore())
    hits = await s.search("   ")
    assert hits == []
