"""Tests for HeartbeatStore + IncidentStore."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.db.migrations import ensure_schema
from core.sentinel.contracts import (
    Category,
    Finding,
    Severity,
    TriageResult,
)
from core.sentinel.heartbeats import HeartbeatStore
from core.sentinel.incidents import IncidentStore


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "pilk.db"
    ensure_schema(p)
    return p


# ── HeartbeatStore ────────────────────────────────────────────


def test_heartbeat_upsert_roundtrip(db: Path) -> None:
    s = HeartbeatStore(db)
    hb = s.upsert(
        agent_name="a", status="ok", progress="doing x", interval_seconds=30
    )
    assert hb.agent_name == "a"
    assert hb.status == "ok"
    assert hb.progress == "doing x"
    fetched = s.get("a")
    assert fetched is not None and fetched.progress == "doing x"


def test_heartbeat_upsert_replaces(db: Path) -> None:
    s = HeartbeatStore(db)
    s.upsert(agent_name="a", status="ok", progress="v1")
    s.upsert(agent_name="a", status="degraded", progress="v2")
    hb = s.get("a")
    assert hb is not None and hb.status == "degraded" and hb.progress == "v2"
    assert len(s.list_all()) == 1


def test_heartbeat_progress_capped_at_160(db: Path) -> None:
    s = HeartbeatStore(db)
    s.upsert(agent_name="a", status="ok", progress="x" * 500)
    hb = s.get("a")
    assert hb is not None and hb.progress is not None
    assert len(hb.progress) == 160


def test_heartbeat_iter_stale_skips_disabled(db: Path) -> None:
    s = HeartbeatStore(db)
    s.upsert(agent_name="a", status="ok", interval_seconds=30)
    s.upsert(agent_name="b", status="disabled", interval_seconds=30)
    # 120s after now; both rows should be "old" but only 'a' yields.
    future = datetime.now(UTC) + timedelta(seconds=120)
    stale = list(s.iter_stale(now=future))
    assert [hb.agent_name for hb in stale] == ["a"]


def test_heartbeat_delete(db: Path) -> None:
    s = HeartbeatStore(db)
    s.upsert(agent_name="a", status="ok")
    assert s.delete("a") is True
    assert s.get("a") is None


# ── IncidentStore ─────────────────────────────────────────────


def _finding() -> Finding:
    return Finding(
        kind="stale_heartbeat",
        agent_name="a",
        summary="a is stale",
        details={"age_seconds": 200},
        dedupe_key="k1",
    )


def _triage() -> TriageResult:
    return TriageResult(
        severity=Severity.HIGH,
        category=Category.STALE_HEARTBEAT,
        likely_cause="crashed",
        recommended_action="restart",
        confidence=0.9,
    )


def test_incident_create_writes_sql_and_jsonl(tmp_path: Path, db: Path) -> None:
    jsonl = tmp_path / "inc.jsonl"
    s = IncidentStore(db_path=db, jsonl_path=jsonl)
    inc = s.create(
        finding=_finding(),
        triage=_triage(),
        category=Category.STALE_HEARTBEAT,
        severity=Severity.HIGH,
        remediation="restarted",
        outcome="ok",
    )
    assert inc.id.startswith("inc-")
    assert jsonl.exists()
    lines = jsonl.read_text().strip().split("\n")
    assert len(lines) == 1
    body = json.loads(lines[0])
    assert body["id"] == inc.id
    assert body["severity"] == "high"


def test_incident_recent_filters_by_min_severity(
    tmp_path: Path, db: Path
) -> None:
    s = IncidentStore(db_path=db, jsonl_path=None)
    s.create(
        finding=_finding(),
        triage=None,
        category=Category.STALE_HEARTBEAT,
        severity=Severity.LOW,
    )
    s.create(
        finding=_finding(),
        triage=None,
        category=Category.CRASH_SIGNATURE,
        severity=Severity.CRITICAL,
    )
    high_only = s.recent(min_severity=Severity.HIGH)
    assert len(high_only) == 1
    assert high_only[0].severity == Severity.CRITICAL


def test_incident_acknowledge(tmp_path: Path, db: Path) -> None:
    s = IncidentStore(db_path=db, jsonl_path=None)
    inc = s.create(
        finding=_finding(),
        triage=None,
        category=Category.STALE_HEARTBEAT,
        severity=Severity.HIGH,
    )
    assert s.acknowledge(inc.id) is True
    # Second ack is idempotent false (no row updated).
    assert s.acknowledge(inc.id) is False


def test_incident_jsonl_write_survives_missing_dir(
    tmp_path: Path, db: Path
) -> None:
    jsonl = tmp_path / "deep" / "nested" / "incidents.jsonl"
    s = IncidentStore(db_path=db, jsonl_path=jsonl)
    s.create(
        finding=_finding(),
        triage=None,
        category=Category.STALE_HEARTBEAT,
        severity=Severity.HIGH,
    )
    assert jsonl.exists()


def test_incident_recent_only_unacked(tmp_path: Path, db: Path) -> None:
    s = IncidentStore(db_path=db, jsonl_path=None)
    a = s.create(
        finding=_finding(),
        triage=None,
        category=Category.STALE_HEARTBEAT,
        severity=Severity.HIGH,
    )
    b = s.create(
        finding=_finding(),
        triage=None,
        category=Category.STALE_HEARTBEAT,
        severity=Severity.HIGH,
    )
    s.acknowledge(a.id)
    unacked = s.recent(only_unacked=True)
    assert [i.id for i in unacked] == [b.id]
