"""Batch 3E tests — operator-pulled intelligence digest.

Verifies:
  - Recent items are returned newest-first
  - ``since`` filter works (ISO timestamp + partial date)
  - ``project`` filter narrows to one project_slug
  - ``include_global`` widens project filter to include null-project items
  - ``limit`` is capped at 200 with a default of 50
  - ``min_score`` filter respects 0-100 bounds
  - ``source`` filter narrows by slug
  - ``topic`` filter matches the score_dimensions_json substring
  - Endpoint is read-only (no DB mutations + no file writes)
  - Existing tests still pass
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.db import connect
from core.db.migrations import ensure_schema
from core.intelligence import ItemStore, SourceRegistry
from core.intelligence.items import DigestEntry


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "pilk_test.db"
    ensure_schema(p)
    return p


# ── helpers ──────────────────────────────────────────────────────


async def _seed_item(
    db_path: Path,
    items: ItemStore,
    *,
    source_id: str,
    title: str,
    url: str,
    score: int = 50,
    matched: dict[str, int] | None = None,
    fetched_at: str | None = None,
) -> str:
    """Create + score an item in one shot. Returns the item id."""
    matched = matched or {}
    stored, _ = await items.upsert_fetched(
        source_id=source_id, title=title, url=url, body="x",
    )
    async with connect(db_path) as conn:
        sets = ["score = ?", "score_reason = ?", "score_dimensions_json = ?", "status = ?"]
        params: list = [
            score,
            f"matched: {','.join(matched.keys())}" if matched else "",
            json.dumps(matched, separators=(",", ":")),
            "scored" if score > 0 else "stored",
        ]
        if fetched_at is not None:
            sets.append("fetched_at = ?")
            params.append(fetched_at)
        params.append(stored.id)
        await conn.execute(
            f"UPDATE intel_items SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await conn.commit()
    return stored.id


# ── 1. Newest-first + default behaviour ─────────────────────────


@pytest.mark.asyncio
async def test_digest_returns_newest_first(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="fee", kind="rss", label="Fee",
        url="https://example.com/f.xml",
    )
    now = datetime.now(UTC)
    a = await _seed_item(
        db_path, items, source_id=s.id, title="oldest",
        url="https://example.com/a", score=50,
        fetched_at=(now - timedelta(hours=2)).isoformat(),
    )
    b = await _seed_item(
        db_path, items, source_id=s.id, title="middle",
        url="https://example.com/b", score=50,
        fetched_at=(now - timedelta(hours=1)).isoformat(),
    )
    c = await _seed_item(
        db_path, items, source_id=s.id, title="newest",
        url="https://example.com/c", score=50,
        fetched_at=now.isoformat(),
    )

    entries = await items.digest()
    assert [e.item_id for e in entries] == [c, b, a]


@pytest.mark.asyncio
async def test_digest_default_limit_is_50_max_200(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="bulk", kind="rss", label="Bulk",
        url="https://example.com/b.xml",
    )
    for i in range(220):
        await _seed_item(
            db_path, items, source_id=s.id,
            title=f"item-{i}",
            url=f"https://example.com/b/{i}",
            score=50,
        )
    # Default limit
    default_entries = await items.digest()
    assert len(default_entries) == 50
    # Explicit limit too high → clamped to 200
    capped = await items.digest(limit=999)
    assert len(capped) == 200
    # Explicit limit too low → clamped to 1
    floored = await items.digest(limit=0)
    assert len(floored) == 1


# ── 2. since filter ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_digest_since_filters_by_iso_timestamp(
    db_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="ti", kind="rss", label="Ti",
        url="https://example.com/t.xml",
    )
    base = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
    old = await _seed_item(
        db_path, items, source_id=s.id, title="old",
        url="https://example.com/o",
        fetched_at=(base - timedelta(days=2)).isoformat(),
    )
    recent = await _seed_item(
        db_path, items, source_id=s.id, title="recent",
        url="https://example.com/r",
        fetched_at=base.isoformat(),
    )
    cutoff = (base - timedelta(hours=1)).isoformat()
    entries = await items.digest(since=cutoff)
    ids = [e.item_id for e in entries]
    assert recent in ids
    assert old not in ids


@pytest.mark.asyncio
async def test_digest_since_accepts_partial_date(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="pd", kind="rss", label="PD",
        url="https://example.com/p.xml",
    )
    await _seed_item(
        db_path, items, source_id=s.id, title="old",
        url="https://example.com/o",
        fetched_at="2026-04-26T12:00:00+00:00",
    )
    keep = await _seed_item(
        db_path, items, source_id=s.id, title="kept",
        url="https://example.com/k",
        fetched_at="2026-04-28T01:00:00+00:00",
    )
    entries = await items.digest(since="2026-04-27")
    assert [e.item_id for e in entries] == [keep]


# ── 3. project + include_global ─────────────────────────────────


@pytest.mark.asyncio
async def test_digest_project_filter_narrows(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    sky = await sources.create(
        slug="sky", kind="rss", label="Sky",
        url="https://example.com/s.xml",
        project_slug="skyway-sales",
    )
    boat = await sources.create(
        slug="bt", kind="rss", label="Bt",
        url="https://example.com/bt.xml",
        project_slug="boat-club",
    )
    glob = await sources.create(
        slug="gl", kind="rss", label="Gl",
        url="https://example.com/g.xml",
    )
    sk = await _seed_item(
        db_path, items, source_id=sky.id, title="sky",
        url="https://example.com/sky",
    )
    bt = await _seed_item(
        db_path, items, source_id=boat.id, title="boat",
        url="https://example.com/bt",
    )
    gl = await _seed_item(
        db_path, items, source_id=glob.id, title="global",
        url="https://example.com/g",
    )
    entries = await items.digest(project="skyway-sales")
    assert [e.item_id for e in entries] == [sk]


@pytest.mark.asyncio
async def test_digest_include_global_widens_project_filter(
    db_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    sky = await sources.create(
        slug="sky", kind="rss", label="Sky",
        url="https://example.com/s.xml",
        project_slug="skyway-sales",
    )
    glob = await sources.create(
        slug="gl", kind="rss", label="Gl",
        url="https://example.com/g.xml",
    )
    boat = await sources.create(
        slug="bt", kind="rss", label="Bt",
        url="https://example.com/bt.xml",
        project_slug="boat-club",
    )
    sk = await _seed_item(
        db_path, items, source_id=sky.id, title="sky",
        url="https://example.com/sky",
    )
    gl = await _seed_item(
        db_path, items, source_id=glob.id, title="global",
        url="https://example.com/g",
    )
    bt = await _seed_item(
        db_path, items, source_id=boat.id, title="boat",
        url="https://example.com/bt",
    )
    entries = await items.digest(
        project="skyway-sales", include_global=True,
    )
    ids = {e.item_id for e in entries}
    assert sk in ids
    assert gl in ids
    assert bt not in ids  # other-project items still excluded


# ── 4. min_score filter ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_digest_min_score_filters(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="ms", kind="rss", label="MS",
        url="https://example.com/m.xml",
    )
    low = await _seed_item(
        db_path, items, source_id=s.id, title="low",
        url="https://example.com/l", score=10,
    )
    mid = await _seed_item(
        db_path, items, source_id=s.id, title="mid",
        url="https://example.com/m", score=50,
    )
    high = await _seed_item(
        db_path, items, source_id=s.id, title="high",
        url="https://example.com/h", score=90,
    )
    entries = await items.digest(min_score=50)
    ids = {e.item_id for e in entries}
    assert low not in ids
    assert mid in ids
    assert high in ids


@pytest.mark.asyncio
async def test_digest_min_score_clamped(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="cl", kind="rss", label="Cl",
        url="https://example.com/c.xml",
    )
    await _seed_item(
        db_path, items, source_id=s.id, title="x",
        url="https://example.com/x", score=10,
    )
    # Wildly over → clamped to 100, no items qualify
    entries = await items.digest(min_score=9999)
    assert entries == []
    # Negative → clamped to 0, all items qualify
    entries2 = await items.digest(min_score=-50)
    assert len(entries2) == 1


# ── 5. source slug filter ───────────────────────────────────────


@pytest.mark.asyncio
async def test_digest_source_slug_filter(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    a = await sources.create(
        slug="a", kind="rss", label="A",
        url="https://example.com/a.xml",
    )
    b = await sources.create(
        slug="b", kind="rss", label="B",
        url="https://example.com/b.xml",
    )
    ai = await _seed_item(
        db_path, items, source_id=a.id, title="A1",
        url="https://example.com/a1",
    )
    bi = await _seed_item(
        db_path, items, source_id=b.id, title="B1",
        url="https://example.com/b1",
    )
    entries = await items.digest(source_slug="a")
    assert [e.item_id for e in entries] == [ai]


# ── 6. topic filter ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_digest_topic_filter(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="tp", kind="rss", label="TP",
        url="https://example.com/t.xml",
    )
    ai = await _seed_item(
        db_path, items, source_id=s.id, title="ai stuff",
        url="https://example.com/ai", score=80,
        matched={"ai-agents": 50, "openai": 30},
    )
    only_oa = await _seed_item(
        db_path, items, source_id=s.id, title="just openai",
        url="https://example.com/oa", score=18,
        matched={"openai": 18},
    )
    other = await _seed_item(
        db_path, items, source_id=s.id, title="other",
        url="https://example.com/o", score=18,
        matched={"news": 18},
    )
    entries = await items.digest(topic="ai-agents")
    ids = {e.item_id for e in entries}
    assert ai in ids
    assert only_oa not in ids
    assert other not in ids


# ── 7. Endpoint is read-only ────────────────────────────────────


@pytest.mark.asyncio
async def test_digest_does_not_mutate_db(db_path: Path) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="ro", kind="rss", label="RO",
        url="https://example.com/r.xml",
    )
    await _seed_item(
        db_path, items, source_id=s.id, title="t",
        url="https://example.com/t", score=80,
    )
    # Snapshot rows
    async with connect(db_path) as conn:
        async with conn.execute(
            "SELECT id, title, url, score, score_dimensions_json, "
            "fetched_at, status FROM intel_items"
        ) as cur:
            before = await cur.fetchall()
    # Run a wide net of digests
    await items.digest()
    await items.digest(since="2000-01-01")
    await items.digest(min_score=10)
    await items.digest(source_slug="ro")
    await items.digest(topic="anything")
    async with connect(db_path) as conn:
        async with conn.execute(
            "SELECT id, title, url, score, score_dimensions_json, "
            "fetched_at, status FROM intel_items"
        ) as cur:
            after = await cur.fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


# ── 8. Route smoke test ─────────────────────────────────────────


def test_route_returns_expected_shape() -> None:
    from fastapi.testclient import TestClient
    from core.api.app import create_app

    app = create_app()
    with TestClient(app) as client:
        r = client.get(
            "/intelligence/digest",
            params={"since": "1970-01-01", "limit": 5, "min_score": 0},
        )
        assert r.status_code == 200
        body = r.json()
        assert "filters" in body
        assert "items" in body
        assert "count" in body
        assert body["filters"]["limit"] == 5
        assert body["filters"]["min_score"] == 0
        if body["count"] > 0:
            entry = body["items"][0]
            for key in [
                "item_id", "title", "url", "source_slug",
                "source_label", "source_kind", "project_slug",
                "published_at", "fetched_at", "score",
                "score_reason", "brain_path", "status",
                "matched_topics",
            ]:
                assert key in entry, f"missing {key} in item entry"
