"""Phase 2 — brain_semantic_search agent tool + HTTP route tests.

Verifies the read-only tool wrapper produces the expected text +
data shape, and that the HTTP route returns 503 cleanly when the
embedder isn't configured (which is the safe default in CI).
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.brain.vector import LocalSQLiteVectorStore, SemanticSearch
from core.brain.vector.indexer import Indexer
from core.brain.vector.tool import make_brain_semantic_search_tool
from core.db.migrations import ensure_schema
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext


class StubEmbedder:
    model = "stub-16"
    dim = 16

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            digest = hashlib.sha256(t.encode("utf-8")).digest()
            v = [((b / 255.0) * 2.0 - 1.0) for b in digest[: self.dim]]
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out

    def estimated_cost_usd(self, total_tokens: int) -> float:
        return 0.0


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "pilk.db"
    ensure_schema(p)
    return p


@pytest.fixture
def brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "PILK-brain"
    (root / "world" / "2026-04-28").mkdir(parents=True)
    (root / "world" / "2026-04-28" / "n.md").write_text(
        "# A note\n\nsome content here for testing.\n",
        encoding="utf-8",
    )
    return root


# ── Tool wrapper ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_returns_data_block(
    db_path: Path, brain_root: Path,
) -> None:
    store = LocalSQLiteVectorStore(db_path)
    embedder = StubEmbedder()
    await Indexer(
        brain_root=brain_root, embedder=embedder, store=store,
    ).index_all()
    tool = make_brain_semantic_search_tool(
        SemanticSearch(embedder=embedder, store=store)
    )
    out = await tool.handler({"query": "anything"}, ToolContext())
    assert not out.is_error
    assert out.data is not None
    assert "hits" in out.data
    assert out.data["count"] >= 1
    assert out.data["filters"]["query"] == "anything"
    # First hit shape includes everything the planner needs to cite.
    h = out.data["hits"][0]
    for key in (
        "brain_path",
        "chunk_idx",
        "heading",
        "content",
        "project_slug",
        "source_type",
        "score",
    ):
        assert key in h


@pytest.mark.asyncio
async def test_tool_empty_query_is_an_error(
    db_path: Path, brain_root: Path,
) -> None:
    store = LocalSQLiteVectorStore(db_path)
    tool = make_brain_semantic_search_tool(
        SemanticSearch(embedder=StubEmbedder(), store=store)
    )
    out = await tool.handler({"query": "  "}, ToolContext())
    assert out.is_error


def test_tool_risk_class_is_read(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    ensure_schema(db)
    store = LocalSQLiteVectorStore(db)
    tool = make_brain_semantic_search_tool(
        SemanticSearch(embedder=StubEmbedder(), store=store)
    )
    assert tool.risk == RiskClass.READ


# ── HTTP route fallback ───────────────────────────────────────────


def _route_test_app():
    """Build a FastAPI app that exposes only the brain router with
    no app.state.semantic_search / brain_indexer set, so the routes
    take the 503 fallback path. Avoids spinning up the full daemon
    lifespan (which would walk the LIVE brain on reindex)."""
    from fastapi import FastAPI

    from core.api.routes.brain import router as brain_router

    app = FastAPI()
    # The brain router already declares ``prefix="/brain"`` so we
    # mount it without an additional prefix.
    app.include_router(brain_router)
    # Deliberately leave semantic_search + brain_indexer unset —
    # the routes must short-circuit to 503 in that case.
    return app


def test_semantic_search_route_503_when_unconfigured() -> None:
    """When the embedder isn't wired (no OPENAI_API_KEY), the route
    must return 503 with a clear message — never a 500, never an
    accidental real-API call."""
    app = _route_test_app()
    with TestClient(app) as client:
        r = client.get("/brain/semantic-search", params={"q": "hello"})
        assert r.status_code == 503
        assert "OPENAI_API_KEY" in r.json().get("detail", "")


def test_reindex_route_503_when_unconfigured() -> None:
    """Same fallback contract for the reindex endpoint — 503 when
    the indexer isn't wired, never a real reindex of the LIVE
    brain (which would cost real money)."""
    app = _route_test_app()
    with TestClient(app) as client:
        r = client.post("/brain/reindex")
        assert r.status_code == 503
        assert "OPENAI_API_KEY" in r.json().get("detail", "")
