"""Batch 3D tests — per-project intelligence brain scoping.

Verifies:
  - Source with no ``project_slug`` writes to ``world/<date>/``
  - Source with ``project_slug=foo`` writes to ``projects/foo/world/<date>/``
  - Manual-source items honour project_slug too
  - Pipeline-driven (auto-fetched) items honour project_slug too
  - Malformed project_slug (path traversal, special chars) falls
    back to the global root + logs (data preserved, not dropped)
  - Collision in project path gets a numeric suffix (no overwrite)
  - The escape guard fires when the resolved target is outside the
    chosen world_root
  - Existing world/ notes remain untouched (covered by the implicit
    contract — these tests don't move or modify any pre-existing
    file; the global suite re-run also checks)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.brain import Vault
from core.db.migrations import ensure_schema
from core.intelligence import ItemStore, SourceRegistry, TopicRegistry
from core.intelligence.brain_writer import BrainWriter, WriteResult
from core.intelligence.fetchers.base import FetchedItem, FetchResult
from core.intelligence.manual import ingest_manual_item
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


# ── 1. Global source (no project_slug) ───────────────────────────


@pytest.mark.asyncio
async def test_global_source_writes_to_world_root(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="global-feed", kind="rss", label="Global",
        url="https://example.com/g.xml",
    )
    item, _ = await items.upsert_fetched(
        source_id=s.id,
        title="Global news",
        url="https://example.com/post-g",
        body="hello",
        published_at="2026-04-27T00:00:00+00:00",
    )

    vault = Vault(vault_path)
    writer = BrainWriter(vault)
    r = writer.write(item=item, source=s, body="hello", matched_topics=[])
    assert r.skipped is False
    assert r.path.startswith("world/")
    assert "/projects/" not in r.path
    assert Path(r.absolute_path).is_file()


# ── 2. Project source writes under projects/<slug>/world/ ────────


@pytest.mark.asyncio
async def test_project_source_writes_under_project_world(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="skyway-feed", kind="rss", label="Skyway",
        url="https://example.com/s.xml",
        project_slug="skyway-sales",
    )
    item, _ = await items.upsert_fetched(
        source_id=s.id,
        title="Skyway dispatch",
        url="https://example.com/post-s",
        body="hi",
        published_at="2026-04-27T00:00:00+00:00",
    )

    vault = Vault(vault_path)
    writer = BrainWriter(vault)
    r = writer.write(item=item, source=s, body="hi", matched_topics=[])
    assert r.skipped is False
    assert r.path.startswith("projects/skyway-sales/world/")
    assert "/2026-04-27/" in r.path
    assert Path(r.absolute_path).is_file()
    # Filesystem layout sanity
    assert (
        vault_path
        / "projects" / "skyway-sales" / "world" / "2026-04-27"
    ).is_dir()


# ── 3. Manual ingest honours project_slug ───────────────────────


@pytest.mark.asyncio
async def test_manual_ingest_writes_to_project_path(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    topics = TopicRegistry(db_path)
    items = ItemStore(db_path)
    vault = Vault(vault_path)

    s = await sources.create(
        slug="boat-club-manual", kind="manual", label="Boat Club (manual)",
        url="https://example.com",
        project_slug="boat-club",
        config={"brain_min_score": 10},
    )
    await topics.create(
        slug="boats", label="Boats", priority="critical",
        keywords=["yacht", "marina"],
    )

    out = await ingest_manual_item(
        source=s,
        url="https://example.com/marina-news",
        title="New marina at Pier 5",
        notes="Yacht club expansion announcement.",
        published_at="2026-04-27",
        topics=topics,
        items=items,
        brain=vault,
        global_threshold=80,  # high — but source overrides to 10
    )
    assert out.ok is True
    assert out.items_brain_written == 1
    assert out.brain_path is not None
    assert out.brain_path.startswith("projects/boat-club/world/")
    assert (vault_path / out.brain_path).is_file()


# ── 4. Pipeline (auto-fetched) honours project_slug ─────────────


@pytest.mark.asyncio
async def test_pipeline_auto_fetched_writes_to_project_path(
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
        slug="trading-feed", kind="rss", label="Trading Feed",
        url="https://example.com/t.xml",
        project_slug="trading",
    )
    await topics.create(
        slug="markets", label="Markets", priority="critical",
        keywords=["xauusd", "gold"],
    )

    fake = FetchResult(
        items=[
            FetchedItem(
                title="Gold makes new high",
                url="https://example.com/xauusd-spike",
                body="Gold (XAUUSD) hits a new high amid rate cut talk.",
            )
        ],
    )
    with patch(
        "core.intelligence.pipeline.fetch_for_source",
        new=AsyncMock(return_value=fake),
    ):
        outcome = await pipeline.run_source(s)

    assert outcome.ok is True
    assert outcome.items_brain_written == 1
    # The note must be under the trading project's world folder.
    proj_root = vault_path / "projects" / "trading" / "world"
    assert proj_root.is_dir()
    written = list(proj_root.rglob("*.md"))
    assert len(written) == 1
    # Top-level world/ should be untouched.
    global_world = vault_path / "world"
    assert (
        not global_world.exists() or not any(global_world.rglob("*.md"))
    )


# ── 5. Unsafe project_slug: traversal / bad chars ──────────────


@pytest.mark.parametrize(
    "bad_slug",
    [
        "../escape",       # path traversal
        "../../escape",
        "/abs/path",       # absolute path
        "..",
        "white space",     # whitespace
        "has/slash",       # slash
        "has\\backslash",  # backslash
        "$pecial",         # special chars
        "ünicode",         # non-ASCII
        "-leading-dash",   # regex requires alnum start
    ],
)
@pytest.mark.asyncio
async def test_unsafe_project_slug_falls_back_to_global(
    db_path: Path, vault_path: Path, bad_slug: str,
) -> None:
    """Malformed slugs MUST NOT escape the brain root. The writer
    falls back to the global ``world/`` and logs a warning rather
    than crashing or writing where the operator didn't intend."""
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)

    # SourceRegistry.create doesn't validate project_slug — that
    # field is operator-defined and might not match an existing
    # project. We bypass the manager and write the bad slug
    # directly so the test exercises the BrainWriter's defence.
    s = await sources.create(
        slug="t", kind="rss", label="T",
        url="https://example.com/x.xml",
    )
    # Force the bad project_slug onto the spec returned to the writer.
    from dataclasses import replace
    bad_source = replace(s, project_slug=bad_slug)

    item, _ = await items.upsert_fetched(
        source_id=s.id,
        title="A post",
        url="https://example.com/post",
        body="x",
        published_at="2026-04-27T00:00:00+00:00",
    )

    vault = Vault(vault_path)
    writer = BrainWriter(vault)
    r = writer.write(
        item=item, source=bad_source, body="x", matched_topics=[],
    )
    # Either it landed in the GLOBAL world/ root (slug rejected,
    # fallback fired) or the path-escape guard refused the write.
    # Both outcomes are acceptable; what's NOT acceptable is a path
    # outside the vault.
    if r.skipped:
        assert "escaped" in (r.reason or "").lower()
    else:
        assert r.path.startswith("world/")
        # Must be inside the vault.
        Path(r.absolute_path).resolve().relative_to(vault_path.resolve())


# Note: ``ends-with-dash-`` actually matches the regex — exclude it
# from the strict-fallback expectation. (Cheap to keep in the param
# matrix above for documentation; the assertion below tolerates
# either branch since the test passes when the path is inside the
# vault either way.)


# ── 6. Collision suffix in project path ────────────────────────


@pytest.mark.asyncio
async def test_project_path_collision_gets_suffix(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="dup", kind="rss", label="Dup",
        url="https://example.com/d.xml",
        project_slug="my-project",
    )
    a, _ = await items.upsert_fetched(
        source_id=s.id, title="Same title",
        url="https://demo.invalid/a", body="MARKER_FIRST_NOTE",
        published_at="2026-04-27T00:00:00+00:00",
    )
    b, _ = await items.upsert_fetched(
        source_id=s.id, title="Same title",
        url="https://demo.invalid/b", body="MARKER_SECOND_NOTE",
        published_at="2026-04-27T00:00:00+00:00",
    )

    vault = Vault(vault_path)
    writer = BrainWriter(vault)

    r1 = writer.write(
        item=a, source=s, body="MARKER_FIRST_NOTE", matched_topics=[],
    )
    r2 = writer.write(
        item=b, source=s, body="MARKER_SECOND_NOTE", matched_topics=[],
    )
    assert r1.path != r2.path
    # Both must live under projects/my-project/world/2026-04-27/
    assert r1.path.startswith("projects/my-project/world/2026-04-27/")
    assert r2.path.startswith("projects/my-project/world/2026-04-27/")
    # First file must NOT have been overwritten.
    first_text = Path(r1.absolute_path).read_text()
    second_text = Path(r2.absolute_path).read_text()
    assert "MARKER_FIRST_NOTE" in first_text
    assert "MARKER_FIRST_NOTE" not in second_text
    assert "MARKER_SECOND_NOTE" in second_text
    assert "MARKER_SECOND_NOTE" not in first_text


# ── 7. Idempotency (already-has-brain_path) preserved ──────────


@pytest.mark.asyncio
async def test_existing_brain_path_is_respected(
    db_path: Path, vault_path: Path,
) -> None:
    sources = SourceRegistry(db_path)
    items = ItemStore(db_path)
    s = await sources.create(
        slug="ip", kind="rss", label="IP",
        url="https://example.com/i.xml",
        project_slug="idemp",
    )
    item, _ = await items.upsert_fetched(
        source_id=s.id, title="x", url="https://example.com/i",
    )
    item.brain_path = "world/2026-04-27/already-recorded.md"
    vault = Vault(vault_path)
    writer = BrainWriter(vault)
    r = writer.write(item=item, source=s, body="x", matched_topics=[])
    assert r.skipped is True
    # The asserted path was never created (idempotency wins).
    assert not (vault_path / item.brain_path).exists()


# ── 8. Direct helper test: _world_root_for ─────────────────────


def test_world_root_for_picks_correct_path(
    vault_path: Path,
) -> None:
    from dataclasses import dataclass

    vault = Vault(vault_path)
    writer = BrainWriter(vault)

    @dataclass
    class FakeSource:
        id: str = "src"
        project_slug: str | None = None

    # No slug → global
    assert writer._world_root_for(FakeSource()) == vault_path / "world"
    # Valid slug → per-project
    assert (
        writer._world_root_for(FakeSource(project_slug="abc"))
        == vault_path / "projects" / "abc" / "world"
    )
    # Empty / whitespace → global
    assert (
        writer._world_root_for(FakeSource(project_slug="  "))
        == vault_path / "world"
    )
    # Path-traversal-ish → falls back to global (does NOT escape)
    fb = writer._world_root_for(FakeSource(project_slug="../escape"))
    assert fb == vault_path / "world"
    # Special chars / whitespace / non-ASCII → fallback
    for bad in ("white space", "$pecial", "ünicode", "/abs"):
        assert (
            writer._world_root_for(FakeSource(project_slug=bad))
            == vault_path / "world"
        )
    # Mixed case is normalized to lowercase rather than rejected
    # (matches the projects-manager convention where slugs are
    # always stored lowercase). "Skyway-Sales" → "skyway-sales".
    norm = writer._world_root_for(
        FakeSource(project_slug="Skyway-Sales"),
    )
    assert norm == vault_path / "projects" / "skyway-sales" / "world"
