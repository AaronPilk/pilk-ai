"""`/logs` aggregator — seed plans/approvals/trust and verify the feed."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.config import get_settings
from core.db import ensure_schema


def _seed(db_path, *, base: datetime) -> None:
    """Write one row into plans / approvals / trust_audit with distinct times."""
    conn = sqlite3.connect(db_path)
    try:
        # plan — oldest
        conn.execute(
            "INSERT INTO plans(id, goal, status, created_at, updated_at, actual_usd) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "plan_aaa",
                "triage my inbox",
                "completed",
                base.isoformat(),
                base.isoformat(),
                0.12,
            ),
        )
        # approval — middle
        appr_at = (base + timedelta(minutes=3)).isoformat()
        conn.execute(
            "INSERT INTO approvals("
            "id, plan_id, step_id, agent_name, risk_class, tool, args_json, "
            "status, created_at, decided_at, decision_reason) "
            "VALUES (?, ?, NULL, NULL, 'COMMS', 'gmail_send', '{}', "
            "'approved', ?, ?, 'looks good')",
            ("appr_bbb", "plan_aaa", appr_at, appr_at),
        )
        # trust rule — newest
        trust_at = (base + timedelta(minutes=7)).isoformat()
        conn.execute(
            "INSERT INTO trust_audit("
            "id, agent_name, tool_name, args_json, ttl_seconds, expires_at, "
            "created_at, created_by, reason) "
            "VALUES (?, NULL, 'gmail_search', '{}', 3600, ?, ?, 'user', "
            "'auto-approve for an hour')",
            (
                "trust_ccc",
                (base + timedelta(hours=1, minutes=7)).isoformat(),
                trust_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client() -> TestClient:
    settings = get_settings()
    ensure_schema(settings.db_path)
    # Use the real app so the route hits the real state.plans.db_path wiring.
    return TestClient(create_app())


def test_logs_feed_merges_and_orders(client: TestClient) -> None:
    settings = get_settings()
    base = datetime.now(UTC) - timedelta(minutes=30)
    _seed(settings.db_path, base=base)

    with client:
        r = client.get("/logs")
    assert r.status_code == 200
    body = r.json()
    entries = body["entries"]
    kinds = [e["kind"] for e in entries]
    # Three rows, desc by time: trust (+7m), approval (+3m), plan (+0m)
    assert kinds == ["trust", "approval", "plan"]
    assert entries[0]["title"] == "gmail_search"
    assert entries[1]["title"] == "gmail_send"
    assert entries[2]["title"] == "triage my inbox"
    assert entries[2]["cost_usd"] == pytest.approx(0.12)


def test_logs_feed_filters_by_kind(client: TestClient) -> None:
    settings = get_settings()
    base = datetime.now(UTC) - timedelta(minutes=30)
    _seed(settings.db_path, base=base)

    with client:
        r = client.get("/logs", params={"kind": "approval"})
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert [e["kind"] for e in entries] == ["approval"]
    assert entries[0]["status"] == "approved"

    with client:
        bad = client.get("/logs", params={"kind": "nope"})
    assert bad.status_code == 400
