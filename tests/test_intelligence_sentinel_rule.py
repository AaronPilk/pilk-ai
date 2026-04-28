"""Batch 3B tests — Intelligence source-health Sentinel rule.

Verifies:
  - No incident when failures are below threshold
  - Incident raised when failures >= threshold
  - Disabled sources are skipped (not surfaced as incidents)
  - Missing intel_sources table → rule returns [] (no crash)
  - Per-source dedupe key is stable across calls
  - Multiple failing sources each get their own Finding
  - Rule recovers cleanly when the source comes back healthy
  - Default threshold falls back to 5 when settings missing
"""

from __future__ import annotations

import sqlite3
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.db.migrations import ensure_schema
from core.intelligence import SourceRegistry
from core.intelligence.sentinel_rules import make_intel_source_health_rule
from core.sentinel.heartbeats import HeartbeatStore
from core.sentinel.rules import RuleContext


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "pilk_test.db"
    ensure_schema(p)
    return p


@pytest.fixture
def heartbeats(db_path: Path) -> HeartbeatStore:
    return HeartbeatStore(db_path)


def _ctx(heartbeats: HeartbeatStore) -> RuleContext:
    return RuleContext(
        heartbeats=heartbeats,
        logs_by_agent={},
        now=datetime.now(UTC),
    )


def _set_failures(db_path: Path, slug: str, failures: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE intel_sources SET consecutive_failures = ? WHERE slug = ?",
            (failures, slug),
        )
        conn.commit()
    finally:
        conn.close()


# ── 1. No incident below threshold ───────────────────────────────


@pytest.mark.asyncio
async def test_no_incident_when_failures_below_threshold(
    db_path: Path, heartbeats: HeartbeatStore,
) -> None:
    sources = SourceRegistry(db_path)
    s = await sources.create(
        slug="ok-feed", kind="rss", label="OK Feed",
        url="https://example.com/ok.xml",
    )
    _set_failures(db_path, s.slug, 2)  # well below threshold of 5

    rule = make_intel_source_health_rule(db_path=db_path, threshold=5)
    findings = await rule(_ctx(heartbeats))
    assert findings == []


# ── 2. Incident at and above threshold ───────────────────────────


@pytest.mark.asyncio
async def test_incident_at_threshold(
    db_path: Path, heartbeats: HeartbeatStore,
) -> None:
    sources = SourceRegistry(db_path)
    s = await sources.create(
        slug="flaky", kind="rss", label="Flaky Feed",
        url="https://example.com/flaky.xml",
    )
    _set_failures(db_path, s.slug, 5)  # exactly at threshold

    rule = make_intel_source_health_rule(db_path=db_path, threshold=5)
    findings = await rule(_ctx(heartbeats))
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "intel_source_health"
    assert f.agent_name == "intel:flaky"
    assert "Flaky Feed" in f.summary
    assert "5 consecutive failures" in f.summary
    assert f.details["slug"] == "flaky"
    assert f.details["consecutive_failures"] == 5
    assert f.details["threshold"] == 5
    assert f.dedupe_key == f"intel_source_health:{s.id}"


@pytest.mark.asyncio
async def test_incident_above_threshold(
    db_path: Path, heartbeats: HeartbeatStore,
) -> None:
    sources = SourceRegistry(db_path)
    await sources.create(
        slug="dead", kind="rss", label="Dead Feed",
        url="https://example.com/dead.xml",
    )
    _set_failures(db_path, "dead", 25)

    rule = make_intel_source_health_rule(db_path=db_path, threshold=5)
    findings = await rule(_ctx(heartbeats))
    assert len(findings) == 1
    assert findings[0].details["consecutive_failures"] == 25


# ── 3. Disabled sources are skipped ──────────────────────────────


@pytest.mark.asyncio
async def test_disabled_sources_do_not_create_incidents(
    db_path: Path, heartbeats: HeartbeatStore,
) -> None:
    sources = SourceRegistry(db_path)
    s = await sources.create(
        slug="off-feed", kind="rss", label="Off Feed",
        url="https://example.com/off.xml",
        enabled=False,
    )
    _set_failures(db_path, s.slug, 50)  # plenty of failures

    rule = make_intel_source_health_rule(db_path=db_path, threshold=5)
    findings = await rule(_ctx(heartbeats))
    assert findings == []


# ── 4. Missing tables degrade gracefully ─────────────────────────


@pytest.mark.asyncio
async def test_missing_intel_tables_returns_empty(
    tmp_path: Path,
) -> None:
    """If intel_sources doesn't exist (e.g. fresh DB before
    migrations), the rule must NOT crash the supervisor."""
    bare_db = tmp_path / "bare.db"
    # Create a DB with only the schema_version table (no intel_*)
    conn = sqlite3.connect(bare_db)
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER, applied_at TEXT)"
    )
    conn.commit()
    conn.close()

    heartbeats_db = tmp_path / "hb.db"
    ensure_schema(heartbeats_db)
    rule = make_intel_source_health_rule(db_path=bare_db, threshold=5)
    findings = await rule(_ctx(HeartbeatStore(heartbeats_db)))
    assert findings == []


# ── 5. Multiple failing sources ──────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_failing_sources_each_have_finding(
    db_path: Path, heartbeats: HeartbeatStore,
) -> None:
    sources = SourceRegistry(db_path)
    s1 = await sources.create(
        slug="a", kind="rss", label="A", url="https://example.com/a.xml",
    )
    s2 = await sources.create(
        slug="b", kind="rss", label="B", url="https://example.com/b.xml",
    )
    s3 = await sources.create(
        slug="healthy", kind="rss", label="Healthy",
        url="https://example.com/h.xml",
    )
    _set_failures(db_path, s1.slug, 7)
    _set_failures(db_path, s2.slug, 12)
    _set_failures(db_path, s3.slug, 0)  # healthy

    rule = make_intel_source_health_rule(db_path=db_path, threshold=5)
    findings = await rule(_ctx(heartbeats))
    slugs = {f.details["slug"] for f in findings}
    assert slugs == {"a", "b"}
    # Findings ordered by severity (most failures first).
    assert findings[0].details["slug"] == "b"
    assert findings[1].details["slug"] == "a"
    # Each has its own dedupe key.
    keys = {f.dedupe_key for f in findings}
    assert len(keys) == 2


# ── 6. Recovery removes the finding ──────────────────────────────


@pytest.mark.asyncio
async def test_recovery_clears_finding(
    db_path: Path, heartbeats: HeartbeatStore,
) -> None:
    """When a source's consecutive_failures drops below threshold
    (because it recovered), the rule stops returning a Finding for
    it. Sentinel's dedupe window then naturally ages out the open
    incident — no explicit 'resolved' call needed (we reuse existing
    Sentinel conventions per Batch 3B spec)."""
    sources = SourceRegistry(db_path)
    s = await sources.create(
        slug="recovers", kind="rss", label="Recovers",
        url="https://example.com/r.xml",
    )

    rule = make_intel_source_health_rule(db_path=db_path, threshold=5)

    _set_failures(db_path, s.slug, 6)
    findings_failing = await rule(_ctx(heartbeats))
    assert len(findings_failing) == 1

    # Source recovers — fetch succeeds, counter resets.
    _set_failures(db_path, s.slug, 0)
    findings_recovered = await rule(_ctx(heartbeats))
    assert findings_recovered == []


# ── 7. Threshold defaults + bounds ───────────────────────────────


def test_default_threshold_is_5() -> None:
    """If the operator hasn't configured the failure-backoff
    threshold, the rule must default to 5 per Batch 3B spec."""
    import inspect
    sig = inspect.signature(make_intel_source_health_rule)
    assert sig.parameters["threshold"].default == 5


@pytest.mark.asyncio
async def test_threshold_bounds_clamp(
    db_path: Path, heartbeats: HeartbeatStore,
) -> None:
    """Threshold of 0 (would spam every source) clamps to >= 1."""
    sources = SourceRegistry(db_path)
    await sources.create(
        slug="never-failed", kind="rss", label="Never failed",
        url="https://example.com/n.xml",
    )
    _set_failures(db_path, "never-failed", 0)
    rule = make_intel_source_health_rule(db_path=db_path, threshold=0)
    findings = await rule(_ctx(heartbeats))
    # Threshold clamped to 1 → 0 failures still doesn't fire
    assert findings == []


# ── 8. Stable dedupe key across calls ────────────────────────────


@pytest.mark.asyncio
async def test_dedupe_key_stable_across_calls(
    db_path: Path, heartbeats: HeartbeatStore,
) -> None:
    sources = SourceRegistry(db_path)
    s = await sources.create(
        slug="stuck", kind="rss", label="Stuck",
        url="https://example.com/s.xml",
    )
    _set_failures(db_path, s.slug, 8)

    rule = make_intel_source_health_rule(db_path=db_path, threshold=5)
    f1 = await rule(_ctx(heartbeats))
    f2 = await rule(_ctx(heartbeats))
    assert f1[0].dedupe_key == f2[0].dedupe_key
    assert f1[0].dedupe_key.startswith("intel_source_health:")
