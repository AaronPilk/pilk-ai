"""System-wide status endpoint.

    GET /system/status
        →
        {
          "version": "0.0.1",
          "cloud_mode": bool,
          "migration_version": int,
          "agents": [{"name": ..., "state": ...}, ...],
          "tools": [<tool names, sorted>],
          "design_tools_present": bool,
          "sentinel_running": bool,
          "home_path": str,
          "uptime_seconds": int,
        }

One fetch that tells the operator *exactly* what's on this deployment.
The point is to diagnose local-vs-cloud drift fast: if the dashboard
says "no agents" and this endpoint agrees, the problem is the deploy;
if this endpoint sees the agents but the dashboard doesn't, the
problem is the frontend wiring.

Public and auth-gated by the existing middleware — same posture as
/agents, /plans, etc. No secrets in the response. Never returns
tokens or keys, only names + high-level state.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Request

from core import __version__
from core.config import get_settings

router = APIRouter(prefix="/system")

_BOOT_TIME = time.time()


def _migration_version(db_path: str) -> int | None:
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] is not None else None


@router.get("/status")
async def system_status(request: Request) -> dict[str, Any]:
    settings = get_settings()
    registry = getattr(request.app.state, "registry", None)
    agents = getattr(request.app.state, "agents", None)
    sentinel = getattr(request.app.state, "sentinel", None)
    client_store = getattr(request.app.state, "clients", None)

    tool_names = sorted(t.name for t in registry.all()) if registry else []

    agent_rows: list[dict[str, Any]] = []
    if agents is not None:
        try:
            agent_rows = [
                {"name": r.get("name"), "state": r.get("state")}
                for r in await agents.list_rows()
            ]
        except Exception:
            agent_rows = []

    # Design-tool presence flag is a quick sanity check — if the
    # dashboard shows "No agents" but this says False, the deploy is
    # behind. If True but dashboard is empty, the frontend is the
    # problem.
    design_tools = {
        "html_export",
        "wordpress_push",
        "elementor_validate",
    }
    design_ok = design_tools.issubset(set(tool_names))

    # Count registered clients from ClientStore — empty in cloud by
    # default, populated via clients/*.yaml.
    client_count = 0
    if client_store is not None:
        try:
            client_count = len(client_store.list())
        except Exception:
            client_count = 0

    return {
        "version": __version__,
        "cloud_mode": settings.cloud,
        "home_path": str(settings.resolve_home()),
        "migration_version": _migration_version(str(settings.db_path)),
        "uptime_seconds": int(time.time() - _BOOT_TIME),
        "agents": agent_rows,
        "agent_count": len(agent_rows),
        "tools": tool_names,
        "tool_count": len(tool_names),
        "design_tools_present": design_ok,
        "client_count": client_count,
        "sentinel_running": (
            sentinel is not None
            and getattr(sentinel, "_scan_task", None) is not None
            and not sentinel._scan_task.done()
        ),
    }
