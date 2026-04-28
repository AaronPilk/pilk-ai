"""/system/status tests — the audit endpoint we rely on to diagnose
local-vs-cloud drift. Single-shot FastAPI TestClient against the full
app factory so the route's shape is exercised end-to-end."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app


@pytest.fixture
def client() -> TestClient:
    # Use `with` so FastAPI runs the lifespan — without this, the app
    # state is empty and the status route returns a shell response.
    with TestClient(create_app()) as c:
        yield c


def test_status_public_no_auth_required(client: TestClient) -> None:
    """/system/status must be reachable without bearer auth — it's
    the one route we hit to diagnose whether auth itself is broken."""
    r = client.get("/system/status")
    assert r.status_code == 200


def test_status_shape(client: TestClient) -> None:
    r = client.get("/system/status")
    body = r.json()
    expected_keys = {
        "version",
        "cloud_mode",
        "home_path",
        "migration_version",
        "uptime_seconds",
        "agents",
        "agent_count",
        "tools",
        "tool_count",
        "design_tools_present",
        "client_count",
        "sentinel_running",
    }
    assert expected_keys.issubset(set(body))


def test_status_lists_registered_tools(client: TestClient) -> None:
    body = client.get("/system/status").json()
    assert isinstance(body["tools"], list)
    assert body["tool_count"] == len(body["tools"])
    # Core built-ins must be present in any boot.
    for name in ("fs_read", "fs_write", "net_fetch", "shell_exec"):
        assert name in body["tools"]


def test_status_confirms_design_stack_present(client: TestClient) -> None:
    body = client.get("/system/status").json()
    # Every tool from the design stack must be registered in main.
    for name in ("html_export", "wordpress_push", "elementor_validate"):
        assert name in body["tools"]
    assert body["design_tools_present"] is True


def test_status_lists_agents(client: TestClient) -> None:
    body = client.get("/system/status").json()
    assert isinstance(body["agents"], list)
    names = {a["name"] for a in body["agents"]}
    # The reference set after the masters consolidation. Specialists
    # were archived into agents/_archive/ — they should NOT show up
    # in registry discovery anymore.
    expected = {
        "master_sales",
        "master_content",
        "master_comms",
        "master_reporting",
        "master_brain",
        "sentinel",
        "xauusd_execution_agent",
    }
    missing = expected - names
    assert not missing, f"missing agents: {missing}"


def test_status_migration_version_reasonable(client: TestClient) -> None:
    body = client.get("/system/status").json()
    # v8 is the last migration we shipped (sentinel_incidents +
    # agent_heartbeats). Anything lower means the deploy is stale.
    assert body["migration_version"] is not None
    assert body["migration_version"] >= 8


def test_status_uptime_monotonic(client: TestClient) -> None:
    a = client.get("/system/status").json()["uptime_seconds"]
    b = client.get("/system/status").json()["uptime_seconds"]
    assert b >= a


def test_status_exposes_cloud_mode_flag(client: TestClient) -> None:
    body = client.get("/system/status").json()
    # Whatever it is, it's a bool — the operator uses this to confirm
    # they're hitting the right deployment.
    assert isinstance(body["cloud_mode"], bool)
