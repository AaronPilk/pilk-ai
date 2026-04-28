"""Batch 2 safety tests for the Intelligence Engine.

Covers:
  - Daemon is disabled by default
  - Manual refresh still works (Batch 1 contract preserved)
  - BrainWriter never overwrites; lands in world/<date>/
  - Duplicate URLs (different UTMs) collapse to one item
  - Mute_until prevents the daemon from picking up a source
  - Existing tests still pass (run via the standard suite)

Tests use temporary directories + the in-process FastAPI TestClient
so nothing here touches the operator's live brain or DB.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.brain import Vault
from core.db.migrations import ensure_schema
from core.intelligence import ItemStore, SourceRegistry, TopicRegistry
from core.intelligence.brain_writer import BrainWriter, MAX_SLUG_CHARS
from core.intelligence.daemon import IntelligenceDaemon
from core.intelligence.fetchers.base import FetchedItem, FetchResult
from core.intelligence.pipeline import IntelligencePipeline
from core.intelligence.scoring import KeywordScorer


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


# ── 1. Daemon-off-by-default ─────────────────────────────────────


@pytest.mark.asyncio
async def test_daemon_disabled_by_default(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=None,
    )
    daemon = IntelligenceDaemon(
        sources=sources, pipeline=pipeline,
        tick_seconds=60, max_concurrent=4,
        backoff_after_failures=5, enabled=False,
    )
    await daemon.start()
    assert daemon.enabled is False
    assert daemon._task is None
    assert daemon.tick_count == 0
    await daemon.stop()


@pytest.mark.asyncio
async def test_daemon_when_enabled_starts_task(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=None,
    )
    daemon = IntelligenceDaemon(
        sources=sources, pipeline=pipeline,
        tick_seconds=60, max_concurrent=4,
        backoff_after_failures=5, enabled=True,
    )
    await daemon.start()
    assert daemon._task is not None
    assert not daemon._task.done()
    await daemon.stop()
    # After stop, the task is cleared.
    assert daemon._task is None


# ── 2. Settings default ─────────────────────────────────────────


def test_intelligence_settings_default_off(monkeypatch) -> None:
    monkeypatch.delenv("PILK_INTELLIGENCE_DAEMON_ENABLED", raising=False)
    monkeypatch.delenv("INTELLIGENCE_DAEMON_ENABLED", raising=False)
    from core.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.intelligence_daemon_enabled is False
    assert s.intelligence_daemon_tick_seconds == 60
    assert s.intelligence_brain_write_threshold == 30


# ── 3. Mute / disabled / due-checking ─────────────────────────


@pytest.mark.asyncio
async def test_daemon_skips_disabled_source(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=None,
    )
    daemon = IntelligenceDaemon(
        sources=sources, pipeline=pipeline, enabled=True,
    )

    s = await sources.create(
        slug="off-feed", kind="rss", label="Off",
        url="https://example.com/off.xml",
        enabled=False,
    )
    # Pipeline should never be called for a disabled source.
    pipeline.run_source = AsyncMock()  # type: ignore[method-assign]
    await daemon._tick_once(now=datetime.now(UTC))
    pipeline.run_source.assert_not_called()


@pytest.mark.asyncio
async def test_daemon_skips_muted_source(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=None,
    )
    daemon = IntelligenceDaemon(
        sources=sources, pipeline=pipeline, enabled=True,
    )

    s = await sources.create(
        slug="muted-feed", kind="rss", label="Muted",
        url="https://example.com/muted.xml",
    )
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    await sources.update(s.id, mute_until=future)

    pipeline.run_source = AsyncMock()  # type: ignore[method-assign]
    await daemon._tick_once(now=datetime.now(UTC))
    pipeline.run_source.assert_not_called()


@pytest.mark.asyncio
async def test_daemon_picks_due_source(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=None,
    )
    daemon = IntelligenceDaemon(
        sources=sources, pipeline=pipeline, enabled=True,
    )
    s = await sources.create(
        slug="due-feed", kind="rss", label="Due",
        url="https://example.com/due.xml",
        poll_interval_seconds=300,
    )
    pipeline.run_source = AsyncMock()  # type: ignore[method-assign]
    await daemon._tick_once(now=datetime.now(UTC))
    # Source has never been checked, so it's due immediately.
    pipeline.run_source.assert_called_once()
    args, kwargs = pipeline.run_source.call_args
    assert args[0].slug == "due-feed"


@pytest.mark.asyncio
async def test_daemon_backoff_extends_interval(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=None,
    )
    daemon = IntelligenceDaemon(
        sources=sources, pipeline=pipeline,
        backoff_after_failures=3, enabled=True,
    )
    s = await sources.create(
        slug="flaky", kind="rss", label="Flaky",
        url="https://example.com/flaky.xml",
        poll_interval_seconds=60,
    )
    # Last checked 2 minutes ago. With no failures, the source IS due
    # (60s interval has elapsed).
    last = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    async with __import__("aiosqlite").connect(db_path) as conn:
        await conn.execute(
            """UPDATE intel_sources
                   SET last_checked_at = ?, consecutive_failures = 0
                 WHERE id = ?""",
            (last, s.id),
        )
        await conn.commit()
    refreshed = await sources.get(s.id)
    assert daemon._is_due(refreshed, now=datetime.now(UTC)) is True

    # Now bump consecutive_failures past the threshold — interval
    # should double, taking it past our 120s elapsed gap.
    async with __import__("aiosqlite").connect(db_path) as conn:
        await conn.execute(
            "UPDATE intel_sources SET consecutive_failures = 5 WHERE id = ?",
            (s.id,),
        )
        await conn.commit()
    refreshed = await sources.get(s.id)
    # excess = 5 - 3 + 1 = 3 → interval *= 2^3 = 480s, > 120s elapsed
    assert daemon._is_due(refreshed, now=datetime.now(UTC)) is False


# ── 4. Brain writer never overwrites ────────────────────────────


@pytest.mark.asyncio
async def test_brain_writer_creates_new_files_only(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    items_store = ItemStore(db_path)
    topics = TopicRegistry(db_path)
    s = await sources.create(
        slug="brand", kind="rss", label="Brand",
        url="https://example.com/brand.xml",
    )

    vault = Vault(vault_path)
    writer = BrainWriter(vault)

    item, _ = await items_store.upsert_fetched(
        source_id=s.id,
        title="My example post",
        url="https://example.com/post-1",
        body="This is the body of the test post.",
    )

    # First write lands at world/<date>/my-example-post.md
    r1 = writer.write(item=item, source=s, body="hello world", matched_topics=[])
    assert r1.skipped is False
    assert "world/" in r1.path
    p1 = Path(r1.absolute_path)
    assert p1.is_file()

    # Mutate item to drop brain_path so we can re-trigger a write
    item.brain_path = None
    # Same title + same day → suffix should appear
    r2 = writer.write(item=item, source=s, body="different body", matched_topics=[])
    assert r2.path != r1.path
    p2 = Path(r2.absolute_path)
    assert p2.is_file()
    # The original file must NOT have been overwritten.
    assert p1.read_text() != p2.read_text()
    assert "hello world" in p1.read_text()
    assert "different body" in p2.read_text()


@pytest.mark.asyncio
async def test_brain_writer_skips_when_already_has_path(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    items_store = ItemStore(db_path)
    s = await sources.create(
        slug="b", kind="rss", label="B",
        url="https://example.com/b.xml",
    )
    vault = Vault(vault_path)
    writer = BrainWriter(vault)

    item, _ = await items_store.upsert_fetched(
        source_id=s.id, title="T", url="https://example.com/t",
    )
    item.brain_path = "world/2026-04-27/already-there.md"
    r = writer.write(item=item, source=s, body="x", matched_topics=[])
    assert r.skipped is True
    # The asserted path should not have been created.
    assert not (vault_path / item.brain_path).exists()


def test_brain_writer_slugify_safe() -> None:
    from core.intelligence.brain_writer import BrainWriter

    # Path-traversal attempts get scrubbed
    assert "/" not in BrainWriter._slugify("../../etc/passwd")
    assert "\\" not in BrainWriter._slugify("foo\\bar")
    # Long title gets capped
    long_title = "a" * 500
    assert len(BrainWriter._slugify(long_title)) <= MAX_SLUG_CHARS
    # Empty / unicode falls back gracefully
    assert BrainWriter._slugify("") == "untitled"
    assert BrainWriter._slugify("   ") == "untitled"


# ── 5. Duplicate URLs collapse ───────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_urls_with_different_utms_dedupe(
    db_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="d", kind="rss", label="D",
        url="https://example.com/d.xml",
    )
    a, new_a = await items.upsert_fetched(
        source_id=s.id, title="Same post",
        url="https://example.com/post?utm_source=a",
        body="x",
    )
    assert new_a is True
    b, new_b = await items.upsert_fetched(
        source_id=s.id, title="Same post",
        url="https://example.com/post?utm_source=b",
        body="x",
    )
    # Different tracking params → same canonical URL → dedup hit
    assert new_b is False
    assert a.id == b.id


# ── 6. Pipeline integrates fetch + score + brain-write ──────────


@pytest.mark.asyncio
async def test_pipeline_writes_brain_on_high_score(
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
        slug="high", kind="rss", label="High",
        url="https://example.com/high.xml",
        default_priority="high",
    )
    await topics.create(
        slug="ai-agents", label="AI agents",
        priority="high",
        keywords=["agent", "claude"],
    )

    # Mock the fetcher to return one synthetic item that matches.
    fake_result = FetchResult(
        items=[
            FetchedItem(
                title="Anthropic ships new Claude agent",
                url="https://example.com/article-1",
                body="Anthropic released a new Claude agent today.",
            )
        ],
        etag="t1",
    )
    with patch(
        "core.intelligence.pipeline.fetch_for_source",
        new=AsyncMock(return_value=fake_result),
    ):
        outcome = await pipeline.run_source(s)

    assert outcome.ok is True
    assert outcome.items_new == 1
    assert outcome.items_brain_written == 1

    # Verify a markdown note actually exists under world/<date>/
    world = vault_path / "world"
    assert world.is_dir()
    written = list(world.rglob("*.md"))
    assert len(written) == 1
    body = written[0].read_text()
    assert "Anthropic ships new Claude agent" in body
    assert "ai-agents" in body  # topic surface in frontmatter


@pytest.mark.asyncio
async def test_pipeline_skips_brain_below_threshold(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    vault = Vault(vault_path)

    pipeline = IntelligencePipeline(
        sources=sources, topics=topics, items=items, brain=vault,
        brain_write_threshold=80,  # very high — keyword score won't clear it
    )

    s = await sources.create(
        slug="low", kind="rss", label="Low",
        url="https://example.com/low.xml",
    )
    await topics.create(
        slug="ai", label="AI", priority="low",
        keywords=["agent"],
    )

    fake_result = FetchResult(
        items=[
            FetchedItem(
                title="An agent post",
                url="https://example.com/x",
                body="Mentions agent once.",
            )
        ],
    )
    with patch(
        "core.intelligence.pipeline.fetch_for_source",
        new=AsyncMock(return_value=fake_result),
    ):
        outcome = await pipeline.run_source(s)

    assert outcome.ok is True
    assert outcome.items_new == 1
    assert outcome.items_brain_written == 0
    # No brain note created
    world = vault_path / "world"
    assert not any(world.rglob("*.md")) if world.exists() else True


# ── 7. KeywordScorer transparency ────────────────────────────────


def test_keyword_scorer_no_topics_returns_zero() -> None:
    s = KeywordScorer([])
    out = s.score(title="Whatever", body="x", url="https://x")
    assert out.score == 0
    assert out.matched_topics == []


def test_keyword_scorer_priority_weighted() -> None:
    from core.intelligence.models import Topic

    high = Topic(
        id="t1", slug="ai", label="AI", priority="high",
        keywords=["agent"],
    )
    low = Topic(
        id="t2", slug="news", label="News", priority="low",
        keywords=["release"],
    )
    s = KeywordScorer([high, low])
    out = s.score(
        title="New agent release", body="", url="https://x",
    )
    # Both topics match once. high contributes 32, low contributes 8.
    assert out.score == 40
    assert "ai" in out.matched_topics
    assert "news" in out.matched_topics
    assert "high" in out.reason or "low" in out.reason


def test_keyword_scorer_caps_at_100() -> None:
    from core.intelligence.models import Topic

    crit = Topic(
        id="t1", slug="x", label="X", priority="critical",
        keywords=["agent", "claude", "ai", "release"],
    )
    s = KeywordScorer([crit])
    out = s.score(
        title="agent claude ai release agent claude ai release",
        body="agent claude ai release",
        url="https://x",
    )
    assert out.score <= 100
