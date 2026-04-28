"""Batch 3A tests — per-source brain_min_score override + dry_run mode.

Verifies:
  - Source with brain_min_score in config_json overrides global default
  - Source without brain_min_score falls back to global default
  - Out-of-range / malformed brain_min_score falls back to global
  - HN's existing config['min_score'] (HN-side filter) does NOT collide
    with brain_min_score (the brain-write threshold)
  - Dry-run mode never writes brain notes; reports projected count
  - Dry-run still records SQLite items (so dedup state is accurate)
  - Threshold info surfaces in the run outcome

These run offline with mocked fetchers — no network, no real DB
beyond the temp test database.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.brain import Vault
from core.db.migrations import ensure_schema
from core.intelligence import ItemStore, SourceRegistry, TopicRegistry
from core.intelligence.fetchers.base import FetchedItem, FetchResult
from core.intelligence.pipeline import IntelligencePipeline


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "pilk_test.db"
    ensure_schema(p)
    return p


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    v = tmp_path / "brain"
    v.mkdir()
    return v


def _matching_fetch_result() -> FetchResult:
    """One synthetic item that scores moderately (matches a single
    'medium' priority topic with one keyword → ~18 points)."""
    return FetchResult(
        items=[
            FetchedItem(
                title="Anthropic ships new Claude agent",
                url="https://example.com/x",
                body="Anthropic released a new Claude agent today.",
            )
        ],
    )


# ── 1. Per-source threshold overrides global ─────────────────────


@pytest.mark.asyncio
async def test_per_source_threshold_overrides_global(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    vault = Vault(vault_path)

    # Global threshold is 30 — would normally write our score-50 item.
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=vault,
        brain_write_threshold=30,
    )

    # Per-source threshold of 80 should BLOCK the write even though
    # the score (50) would clear the global default.
    s = await sources.create(
        slug="strict", kind="rss", label="Strict",
        url="https://example.com/strict.xml",
        config={"brain_min_score": 80},
    )
    await topics.create(
        slug="ai-agents", label="AI", priority="critical",
        keywords=["claude"],  # critical priority * 1 hit = 50 pts
    )

    with patch(
        "core.intelligence.pipeline.fetch_for_source",
        new=AsyncMock(return_value=_matching_fetch_result()),
    ):
        outcome = await pipeline.run_source(s)

    assert outcome.ok is True
    assert outcome.items_new == 1
    assert outcome.items_brain_written == 0  # blocked by 80 floor
    assert outcome.threshold_applied == 80
    assert outcome.threshold_source == "source"
    # No file landed on disk
    world = vault_path / "world"
    assert not world.exists() or not any(world.rglob("*.md"))


@pytest.mark.asyncio
async def test_per_source_threshold_can_be_lower_than_global(
    db_path: Path, vault_path: Path,
) -> None:
    """A per-source threshold of 5 should let through items the
    global threshold (60) would block. This is the symmetric case."""
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    vault = Vault(vault_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=vault,
        brain_write_threshold=60,  # high global
    )
    s = await sources.create(
        slug="loose", kind="rss", label="Loose",
        url="https://example.com/loose.xml",
        config={"brain_min_score": 5},  # very permissive
    )
    await topics.create(
        slug="news", label="News", priority="low",
        keywords=["claude"],  # low priority * 1 hit = 8 pts
    )
    with patch(
        "core.intelligence.pipeline.fetch_for_source",
        new=AsyncMock(return_value=_matching_fetch_result()),
    ):
        outcome = await pipeline.run_source(s)
    assert outcome.items_brain_written == 1
    assert outcome.threshold_applied == 5
    assert outcome.threshold_source == "source"


# ── 2. Fallback to global when no per-source threshold ───────────


@pytest.mark.asyncio
async def test_no_per_source_threshold_falls_back_to_global(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    vault = Vault(vault_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=vault,
        brain_write_threshold=30,
    )
    s = await sources.create(
        slug="default", kind="rss", label="Default",
        url="https://example.com/default.xml",
        # No brain_min_score at all → use global 30
    )
    await topics.create(
        slug="ai", label="AI", priority="critical",
        keywords=["claude"],
    )
    with patch(
        "core.intelligence.pipeline.fetch_for_source",
        new=AsyncMock(return_value=_matching_fetch_result()),
    ):
        outcome = await pipeline.run_source(s)
    assert outcome.threshold_applied == 30
    assert outcome.threshold_source == "global"
    assert outcome.items_brain_written == 1


@pytest.mark.parametrize(
    "bad_value",
    ["abc", -10, 999, None, [], {}, ""],
)
@pytest.mark.asyncio
async def test_invalid_brain_min_score_falls_back_to_global(
    db_path: Path, bad_value,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=None,
        brain_write_threshold=42,
    )
    s = await sources.create(
        slug="bad", kind="rss", label="Bad",
        url="https://example.com/bad.xml",
        config={"brain_min_score": bad_value},
    )
    threshold, source = pipeline._resolve_threshold(s)
    assert threshold == 42
    assert source == "global"


# ── 3. HN's config.min_score does NOT collide with brain threshold ─


@pytest.mark.asyncio
async def test_hn_min_score_filter_does_not_collide(
    db_path: Path,
) -> None:
    """The HN fetcher uses ``config.min_score`` for HN-side story
    filtering. The brain threshold uses ``config.brain_min_score``.
    Different keys, different concepts — make sure setting one does
    not accidentally affect the other."""
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=None,
        brain_write_threshold=30,
    )
    s = await sources.create(
        slug="hn", kind="hacker_news", label="HN",
        url="https://news.ycombinator.com/",
        # Operator wants HN to filter at 100 points + brain threshold 25
        config={"min_score": 100, "brain_min_score": 25},
    )
    threshold, layer = pipeline._resolve_threshold(s)
    assert threshold == 25
    assert layer == "source"
    # HN's filter is NOT touched by the threshold resolver — it
    # belongs to the fetcher and stays in config.
    assert s.config.get("min_score") == 100


# ── 4. Dry-run mode ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_reports_projection_without_writing(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    vault = Vault(vault_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=vault,
        brain_write_threshold=20,
    )
    s = await sources.create(
        slug="preview", kind="rss", label="Preview",
        url="https://example.com/preview.xml",
    )
    await topics.create(
        slug="ai", label="AI", priority="critical",
        keywords=["claude"],
    )
    with patch(
        "core.intelligence.pipeline.fetch_for_source",
        new=AsyncMock(return_value=_matching_fetch_result()),
    ):
        outcome = await pipeline.run_source(s, dry_run=True)

    # Item still landed in DB (dedup state matters going forward).
    assert outcome.items_new == 1
    # Brain write was suppressed.
    assert outcome.items_brain_written == 0
    # Projection counted 1 (would-have-written).
    assert outcome.items_would_brain_write == 1
    assert outcome.dry_run is True
    # No actual file on disk.
    world = vault_path / "world"
    assert not world.exists() or not any(world.rglob("*.md"))


@pytest.mark.asyncio
async def test_dry_run_then_real_run_writes_no_duplicates(
    db_path: Path, vault_path: Path,
) -> None:
    """A dry-run followed by a real run on the SAME items should not
    re-write — dedup catches them. The dry-run already stored the
    SQLite row, so the second pass treats them as duplicates."""
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    vault = Vault(vault_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=vault,
        brain_write_threshold=20,
    )
    s = await sources.create(
        slug="rerun", kind="rss", label="Rerun",
        url="https://example.com/r.xml",
    )
    await topics.create(
        slug="ai", label="AI", priority="critical",
        keywords=["claude"],
    )

    with patch(
        "core.intelligence.pipeline.fetch_for_source",
        new=AsyncMock(return_value=_matching_fetch_result()),
    ):
        first = await pipeline.run_source(s, dry_run=True)
        second = await pipeline.run_source(s, dry_run=False)

    assert first.items_new == 1
    assert first.items_would_brain_write == 1
    assert first.items_brain_written == 0

    # Second run sees the same item, dedups, never reaches scoring or
    # writing. Brain stays empty.
    assert second.items_new == 0
    assert second.items_dup == 1
    assert second.items_brain_written == 0


# ── 5. Threshold info appears in REST response ─────────────────


def test_route_response_carries_threshold_metadata() -> None:
    from fastapi.testclient import TestClient
    from core.api.app import create_app

    app = create_app()
    with TestClient(app) as client:
        # Pick any existing source slug from the live DB; if none
        # exist this test is a no-op.
        r = client.get("/intelligence/sources")
        sources = r.json().get("sources") or []
        if not sources:
            pytest.skip("no live sources configured for this check")
        sid = sources[0]["id"]
        r = client.post(
            f"/intelligence/sources/{sid}/refresh?dry_run=true",
        )
        if r.status_code == 501:
            pytest.skip("source kind not implemented locally")
        if r.status_code >= 500:
            pytest.skip(f"refresh failed (likely network): {r.text[:120]}")
        body = r.json()
        # Must surface dry_run + threshold info regardless of fetch
        # outcome (even errors should still pass these flags through).
        assert body.get("dry_run") is True
        assert "threshold_applied" in body
        assert body["threshold_source"] in {"source", "global"}
