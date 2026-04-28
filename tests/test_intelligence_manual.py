"""Batch 3C tests — operator-curated manual ingest.

Verifies:
  - Manual item accepted, scored, and (above threshold) written to brain
  - Duplicate URL → no duplicate item, no second brain write
  - Non-manual sources reject POST /items with 400
  - Failed page fetch still stores the URL + operator notes
  - Operator-supplied title wins over fetched title
  - URL-derived fallback used when both are missing
  - Per-source brain_min_score honored
  - Daemon skips manual sources
  - Existing tests still pass (covered by full-suite run)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from core.brain import Vault
from core.db.migrations import ensure_schema
from core.intelligence import ItemStore, SourceRegistry, TopicRegistry
from core.intelligence.daemon import IntelligenceDaemon
from core.intelligence.manual import (
    ManualIngestOutcome,
    ingest_manual_item,
)
from core.intelligence.pipeline import IntelligencePipeline


# ── fixtures ────────────────────────────────────────────────────


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


def _mock_http(*, status: int, body: bytes, content_type: str = "text/html") -> httpx.AsyncClient:
    """Tiny httpx client double for the title-extractor's fetch
    step. Avoids hitting the network in tests."""
    class _Resp:
        def __init__(self) -> None:
            self.status_code = status
            self.content = body
            self.headers = {"content-type": content_type}

    class _Client:
        async def get(self, *a, **k) -> _Resp:  # noqa: D401
            return _Resp()

        async def aclose(self) -> None:
            pass

    return _Client()  # type: ignore[return-value]


def _failing_http() -> httpx.AsyncClient:
    class _Client:
        async def get(self, *a, **k):
            raise httpx.ConnectError("simulated DNS failure")

        async def aclose(self) -> None:
            pass

    return _Client()  # type: ignore[return-value]


# ── 1. Happy path: manual item accepted ────────────────────────


@pytest.mark.asyncio
async def test_manual_item_accepted_and_scored(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    vault = Vault(vault_path)

    s = await sources.create(
        slug="anthropic-manual", kind="manual",
        label="Anthropic (manual)",
        url="https://www.anthropic.com",
        config={"brain_min_score": 20},
    )
    await topics.create(
        slug="anthropic", label="Anthropic",
        priority="critical",
        keywords=["anthropic", "claude"],
    )

    out = await ingest_manual_item(
        source=s,
        url="https://www.anthropic.com/news/claude-3-5-sonnet",
        title="Introducing Claude 3.5 Sonnet",
        notes="Major release — new tool use capabilities.",
        published_at="2026-04-27",
        topics=topics,
        items=items,
        brain=vault,
        global_threshold=30,
    )
    assert out.ok is True
    assert out.items_new == 1
    assert out.items_dup == 0
    assert out.items_brain_written == 1
    assert out.title_source == "operator"
    assert out.fetch_attempted is False
    assert out.score is not None and out.score >= 50
    assert out.threshold_applied == 20
    assert out.threshold_source == "source"
    assert "anthropic" in (out.matched_topics or [])
    assert out.brain_path is not None and "world/" in out.brain_path

    # Brain note was actually written.
    assert (vault_path / out.brain_path).is_file()


# ── 2. Dedup: same URL → no duplicate item ─────────────────────


@pytest.mark.asyncio
async def test_duplicate_url_does_not_create_duplicate(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    vault = Vault(vault_path)

    s = await sources.create(
        slug="anthropic-manual", kind="manual",
        label="Anthropic (manual)",
        url="https://www.anthropic.com",
    )
    await topics.create(
        slug="anthropic", label="Anthropic",
        priority="critical", keywords=["claude"],
    )

    # First submission with one set of UTM params
    a = await ingest_manual_item(
        source=s,
        url="https://www.anthropic.com/news/post?utm_source=op",
        title="Claude post",
        notes=None,
        published_at=None,
        topics=topics, items=items, brain=vault,
        global_threshold=30,
    )
    assert a.items_new == 1

    # Second submission — different UTM, same canonical URL
    b = await ingest_manual_item(
        source=s,
        url="https://www.anthropic.com/news/post?utm_source=elsewhere",
        title="Claude post (re-shared)",
        notes="A note this time",
        published_at=None,
        topics=topics, items=items, brain=vault,
        global_threshold=30,
    )
    assert b.items_new == 0
    assert b.items_dup == 1
    assert b.items_brain_written == 0
    assert b.item_id == a.item_id


# ── 3. Non-manual source rejects /items via the route ──────────


def test_non_manual_source_rejects_items_post() -> None:
    from fastapi.testclient import TestClient
    from core.api.app import create_app

    app = create_app()
    with TestClient(app) as client:
        # Create a non-manual source (rss).
        r = client.post(
            "/intelligence/sources",
            json={
                "slug": "test-rss-rejects-items",
                "kind": "rss",
                "label": "RSS test",
                "url": "https://example.com/feed.xml",
            },
        )
        if r.status_code == 400 and "already exists" in r.text:
            # Leftover from a previous run — clean it up first
            r = client.get("/intelligence/sources")
            sid = next(
                s["id"] for s in r.json()["sources"]
                if s["slug"] == "test-rss-rejects-items"
            )
            client.delete(f"/intelligence/sources/{sid}")
            r = client.post(
                "/intelligence/sources",
                json={
                    "slug": "test-rss-rejects-items",
                    "kind": "rss",
                    "label": "RSS test",
                    "url": "https://example.com/feed.xml",
                },
            )
        assert r.status_code == 200
        sid = r.json()["id"]
        try:
            r = client.post(
                f"/intelligence/sources/{sid}/items",
                json={"url": "https://example.com/x"},
            )
            assert r.status_code == 400
            detail = r.json()["detail"]
            assert "not 'manual'" in detail
            assert "kind 'rss'" in detail
        finally:
            client.delete(f"/intelligence/sources/{sid}")


# ── 4. Failed page fetch — still stores submission ─────────────


@pytest.mark.asyncio
async def test_failed_fetch_still_stores_with_url_fallback(
    db_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)

    s = await sources.create(
        slug="dead-link-manual", kind="manual",
        label="Dead Link Manual",
        url="https://example.com",
    )

    # No title supplied, fetch fails — should fall back to URL.
    out = await ingest_manual_item(
        source=s,
        url="https://no-such-host.invalid/article-42",
        title=None,
        notes="Found via Aaron's RSS reader, want to revisit",
        published_at=None,
        topics=topics, items=items, brain=None,  # no brain wiring needed
        global_threshold=30,
        http=_failing_http(),
    )
    assert out.ok is True
    assert out.items_new == 1
    assert out.fetch_attempted is True
    assert out.fetch_succeeded is False
    assert out.fetch_error is not None
    assert out.title_source == "url-fallback"
    assert "no-such-host.invalid" in (out.title_used or "")


@pytest.mark.asyncio
async def test_fetch_succeeds_uses_extracted_title(
    db_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="m", kind="manual", label="M", url="https://x.com",
    )

    page = (
        b"<html><head>"
        b"<title>Operator title from page</title>"
        b'<meta property="og:title" content="OG title wins">'
        b"</head><body>...</body></html>"
    )
    out = await ingest_manual_item(
        source=s,
        url="https://example.com/path",
        title=None,
        notes=None,
        published_at=None,
        topics=topics, items=items, brain=None,
        global_threshold=30,
        http=_mock_http(status=200, body=page),
    )
    assert out.fetch_succeeded is True
    assert out.title_source == "fetched"
    # OG > <title> per the picker's order.
    assert out.title_used == "OG title wins"


@pytest.mark.asyncio
async def test_operator_title_overrides_fetch(
    db_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="m2", kind="manual", label="M2",
        url="https://x.com",
    )
    out = await ingest_manual_item(
        source=s,
        url="https://example.com/article",
        title="My carefully chosen title",
        notes=None,
        published_at=None,
        topics=topics, items=items, brain=None,
        global_threshold=30,
        # http is not used — operator title short-circuits the fetch
        http=_mock_http(status=200, body=b"<title>Different</title>"),
    )
    assert out.title_source == "operator"
    assert out.title_used == "My carefully chosen title"
    assert out.fetch_attempted is False


# ── 5. Per-source brain_min_score is honored ───────────────────


@pytest.mark.asyncio
async def test_per_source_threshold_blocks_low_score(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    vault = Vault(vault_path)

    s = await sources.create(
        slug="picky-manual", kind="manual",
        label="Picky",
        url="https://example.com",
        config={"brain_min_score": 80},  # high bar
    )
    await topics.create(
        slug="ai", label="AI", priority="medium",
        keywords=["claude"],  # one match → ~18 pts at medium
    )

    out = await ingest_manual_item(
        source=s,
        url="https://example.com/claude-update",
        title="Claude update",
        notes=None,
        published_at=None,
        topics=topics, items=items, brain=vault,
        global_threshold=10,  # global is permissive; source override blocks
    )
    assert out.ok is True
    assert out.items_new == 1
    assert out.items_brain_written == 0  # blocked by 80 floor
    assert out.threshold_applied == 80
    assert out.threshold_source == "source"


@pytest.mark.asyncio
async def test_global_fallback_when_no_source_threshold(
    db_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="default-manual", kind="manual",
        label="Default",
        url="https://example.com",
    )
    out = await ingest_manual_item(
        source=s,
        url="https://example.com/x",
        title="x",
        notes=None,
        published_at=None,
        topics=topics, items=items, brain=None,
        global_threshold=42,
    )
    assert out.threshold_applied == 42
    assert out.threshold_source == "global"


# ── 6. Daemon skips manual sources ─────────────────────────────


@pytest.mark.asyncio
async def test_daemon_skips_manual_sources(db_path: Path) -> None:
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
        slug="skip-me", kind="manual", label="Skip Me",
        url="https://example.com",
    )
    pipeline.run_source = AsyncMock()  # type: ignore[method-assign]
    from datetime import UTC, datetime
    await daemon._tick_once(now=datetime.now(UTC))
    pipeline.run_source.assert_not_called()


# ── 7. Manual sources also rejected by /refresh fetcher ────────


@pytest.mark.asyncio
async def test_refresh_on_manual_source_returns_clear_error(
    db_path: Path,
) -> None:
    """Defence in depth: if someone POSTs /refresh on a manual
    source, the fetcher dispatcher refuses with NotImplementedFetchError
    so the operator gets a 501 with a useful pointer to /items."""
    from core.intelligence.fetchers import (
        NotImplementedFetchError,
        fetch_for_source,
    )
    sources = SourceRegistry(db_path)
    s = await sources.create(
        slug="m-refresh", kind="manual",
        label="M",
        url="https://example.com",
    )
    with pytest.raises(NotImplementedFetchError) as excinfo:
        await fetch_for_source(s)
    assert "POST /intelligence/sources" in str(excinfo.value)
