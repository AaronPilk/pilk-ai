"""Batch 3F tests — operator-pulled intelligence brief tooling.

Verifies:
  - ``intelligence_digest_read`` tool returns digest data shaped for
    Master Reporting (text content + structured ``data`` block)
  - Tool is read-only (no DB mutations)
  - All filter params plumb through to the underlying ItemStore
  - Tool risk class is READ (auto-allowed, no approval prompt)
  - Master Reporting manifest declares the tool in its allowlist
  - Master Reporting manifest carries the new "DAILY / WEEKLY
    INTELLIGENCE BRIEF" playbook section
  - Tool gets registered through the live FastAPI lifespan
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.db import connect
from core.db.migrations import ensure_schema
from core.intelligence import ItemStore, SourceRegistry
from core.intelligence.tools import make_intelligence_digest_read_tool
from core.policy.risk import RiskClass
from core.tools.registry import ToolContext


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "pilk_test.db"
    ensure_schema(p)
    return p


async def _seed_items(db_path: Path) -> tuple[str, str]:
    """Build a tiny world: one global source + one project source,
    one item each. Returns (global_item_id, project_item_id)."""
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)

    glob = await sources.create(
        slug="g", kind="rss", label="Global Feed",
        url="https://example.com/g.xml",
    )
    proj = await sources.create(
        slug="p", kind="rss", label="Project Feed",
        url="https://example.com/p.xml",
        project_slug="skyway-sales",
    )
    g_item, _ = await items.upsert_fetched(
        source_id=glob.id, title="Global story",
        url="https://example.com/g/1", body="x",
    )
    p_item, _ = await items.upsert_fetched(
        source_id=proj.id, title="Project story",
        url="https://example.com/p/1", body="y",
    )
    # Score them so the digest fields are populated
    async with connect(db_path) as conn:
        await conn.execute(
            "UPDATE intel_items SET score = ?, score_reason = ?, "
            "score_dimensions_json = ?, status = 'scored' "
            "WHERE id = ?",
            (
                72,
                "matched ai-agents(high): 2 hits",
                json.dumps({"ai-agents": 60, "openai": 12}),
                g_item.id,
            ),
        )
        await conn.execute(
            "UPDATE intel_items SET score = ?, score_reason = ?, "
            "score_dimensions_json = ?, status = 'scored' "
            "WHERE id = ?",
            (
                40,
                "matched anthropic(critical): 1 hit",
                json.dumps({"anthropic-claude": 40}),
                p_item.id,
            ),
        )
        await conn.commit()
    return g_item.id, p_item.id


# ── 1. Tool returns read-only digest data ──────────────────────


@pytest.mark.asyncio
async def test_tool_returns_digest_payload(db_path: Path) -> None:
    g_id, p_id = await _seed_items(db_path)
    tool = make_intelligence_digest_read_tool(db_path)
    out = await tool.handler({"limit": 10}, ToolContext())
    assert not out.is_error
    assert out.data is not None
    assert "items" in out.data
    assert out.data["count"] == 2
    titles = {i["title"] for i in out.data["items"]}
    assert "Global story" in titles
    assert "Project story" in titles
    # Text rendering surfaces titles + URLs for the planner.
    assert "Global story" in out.content
    assert "Project story" in out.content
    assert "https://example.com/g/1" in out.content


# ── 2. Filters plumb through ───────────────────────────────────


@pytest.mark.asyncio
async def test_tool_project_filter(db_path: Path) -> None:
    _, p_id = await _seed_items(db_path)
    tool = make_intelligence_digest_read_tool(db_path)
    out = await tool.handler({"project": "skyway-sales"}, ToolContext())
    assert out.data["count"] == 1
    assert out.data["items"][0]["item_id"] == p_id


@pytest.mark.asyncio
async def test_tool_include_global_widens_project(db_path: Path) -> None:
    g_id, p_id = await _seed_items(db_path)
    tool = make_intelligence_digest_read_tool(db_path)
    out = await tool.handler(
        {"project": "skyway-sales", "include_global": True},
        ToolContext(),
    )
    ids = {i["item_id"] for i in out.data["items"]}
    assert g_id in ids
    assert p_id in ids


@pytest.mark.asyncio
async def test_tool_min_score_filter(db_path: Path) -> None:
    g_id, p_id = await _seed_items(db_path)
    tool = make_intelligence_digest_read_tool(db_path)
    out = await tool.handler({"min_score": 50}, ToolContext())
    ids = {i["item_id"] for i in out.data["items"]}
    assert g_id in ids       # score 72
    assert p_id not in ids   # score 40 — filtered out


@pytest.mark.asyncio
async def test_tool_topic_filter(db_path: Path) -> None:
    g_id, _ = await _seed_items(db_path)
    tool = make_intelligence_digest_read_tool(db_path)
    out = await tool.handler({"topic": "ai-agents"}, ToolContext())
    assert out.data["count"] == 1
    assert out.data["items"][0]["item_id"] == g_id


@pytest.mark.asyncio
async def test_tool_since_filter(db_path: Path) -> None:
    await _seed_items(db_path)
    # Pick a future cutoff — nothing matches.
    tomorrow = (
        (datetime.now(UTC) + timedelta(days=1)).isoformat()
    )
    tool = make_intelligence_digest_read_tool(db_path)
    out = await tool.handler({"since": tomorrow}, ToolContext())
    assert out.data["count"] == 0
    assert "0 intelligence items" in out.content


@pytest.mark.asyncio
async def test_tool_limit_clamped(db_path: Path) -> None:
    await _seed_items(db_path)
    tool = make_intelligence_digest_read_tool(db_path)
    # Wildly over → clamped to 200
    out = await tool.handler({"limit": 9999}, ToolContext())
    assert out.data["filters"]["limit"] == 200
    # Zero → clamped to 1
    out2 = await tool.handler({"limit": 0}, ToolContext())
    assert out2.data["filters"]["limit"] == 1


@pytest.mark.asyncio
async def test_tool_invalid_limit_falls_back_to_default(
    db_path: Path,
) -> None:
    await _seed_items(db_path)
    tool = make_intelligence_digest_read_tool(db_path)
    out = await tool.handler({"limit": "not-a-number"}, ToolContext())
    assert not out.is_error
    assert out.data["filters"]["limit"] == 50


# ── 3. Tool is read-only ───────────────────────────────────────


def test_tool_risk_class_is_read(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    ensure_schema(db)
    tool = make_intelligence_digest_read_tool(db)
    assert tool.risk == RiskClass.READ


@pytest.mark.asyncio
async def test_tool_does_not_mutate_db(db_path: Path) -> None:
    g_id, p_id = await _seed_items(db_path)

    async def _snapshot() -> list:
        async with connect(db_path) as conn:
            async with conn.execute(
                "SELECT id, title, url, score, score_dimensions_json, "
                "fetched_at, status FROM intel_items ORDER BY id"
            ) as cur:
                return [tuple(r) for r in await cur.fetchall()]

    before = await _snapshot()
    tool = make_intelligence_digest_read_tool(db_path)
    # Run the tool with a wide variety of filter combos.
    for args in [
        {},
        {"limit": 100},
        {"min_score": 0},
        {"min_score": 90},
        {"project": "skyway-sales"},
        {"project": "nonexistent"},
        {"include_global": True},
        {"source": "g"},
        {"topic": "ai-agents"},
        {"since": "1970-01-01"},
        {"since": "9999-12-31"},
    ]:
        await tool.handler(args, ToolContext())
    after = await _snapshot()
    assert before == after


# ── 4. Master Reporting manifest carries the new contract ──────


def test_master_reporting_declares_intelligence_tool() -> None:
    from core.registry.manifest import Manifest

    m = Manifest.load(
        Path(__file__).resolve().parents[1]
        / "agents" / "master_reporting" / "manifest.yaml"
    )
    assert "intelligence_digest_read" in m.tools


def test_master_reporting_has_intel_brief_playbook() -> None:
    from core.registry.manifest import Manifest

    m = Manifest.load(
        Path(__file__).resolve().parents[1]
        / "agents" / "master_reporting" / "manifest.yaml"
    )
    sp = m.system_prompt
    assert "DAILY / WEEKLY INTELLIGENCE BRIEF" in sp
    assert "intelligence_digest_read" in sp
    # Must spell out: operator-pulled only, no auto-fire
    assert (
        "Operator-pulled only" in sp
        or "operator-pulled only" in sp
        or "never auto-fire" in sp
        or "Never auto-fire" in sp
    )
    # Playbook explicitly forbids autonomous side effects
    for forbidden in (
        "never auto-execute",
        "Do NOT call code_task",
        "do NOT spawn",
        "do NOT send Telegram",
    ):
        assert forbidden in sp or forbidden.lower() in sp.lower()


# ── 5. Tool registers via the live FastAPI app ────────────────


def test_tool_registered_at_app_boot() -> None:
    from fastapi.testclient import TestClient
    from core.api.app import create_app

    app = create_app()
    with TestClient(app):
        registry = app.state.registry
        names = {t.name for t in registry.all()}
        assert "intelligence_digest_read" in names
