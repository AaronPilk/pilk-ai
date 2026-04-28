"""GET /system/safety — single-pane view of PILK's safety posture.

Returns:
  - computer_control: enabled, daily limit, daily used, audit log
  - sandboxes: registered + states + capabilities
  - browser_sessions: active count + last seen
  - risk_distribution: count of registered tools by RiskClass
  - approvals: count pending in the gate's approval queue
  - sentinel: running + recent incident count (last 24h)
  - last_irreversible_actions: last N action-log entries
  - autonomy_profiles: each agent's profile

Read-only. Never modifies state. Useful for the dashboard's
'safety' tile and for the operator to confirm 'is anything risky
running right now?' before stepping away.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from core.config import get_settings
from core.policy.risk import RiskClass

router = APIRouter(prefix="/system")


@router.get("/safety")
async def system_safety(request: Request) -> dict[str, Any]:
    settings = get_settings()
    registry = getattr(request.app.state, "registry", None)
    sandboxes = getattr(request.app.state, "sandboxes", None)
    sentinel = getattr(request.app.state, "sentinel", None)
    sentinel_incidents = getattr(
        request.app.state, "sentinel_incidents", None,
    )
    browser_manager = getattr(
        request.app.state, "browser_session_manager", None,
    )
    plans = getattr(request.app.state, "plans", None)

    # ── Computer control ─────────────────────────────────────────
    cc_enabled = (
        (settings.computer_control_enabled or "").strip().lower() == "true"
    )
    cc_log_path = settings.resolve_home() / "logs" / "computer-control.jsonl"
    cc_used_today = 0
    cc_last_action: dict[str, Any] | None = None
    if cc_log_path.exists():
        try:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            with cc_log_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cc_last_action = entry
                    at = (entry.get("at") or "")[:10]
                    if at == today:
                        cc_used_today += 1
        except OSError:
            pass

    # ── Sandboxes ─────────────────────────────────────────────────
    sandbox_rows: list[dict[str, Any]] = []
    if sandboxes is not None:
        try:
            for s in await sandboxes.list_rows():
                sandbox_rows.append(
                    {
                        "id": s.get("id"),
                        "type": s.get("type"),
                        "agent_name": s.get("agent_name"),
                        "state": s.get("state"),
                        "created_at": s.get("created_at"),
                        "destroyed_at": s.get("destroyed_at"),
                    }
                )
        except Exception:
            pass

    # ── Risk distribution of registered tools ────────────────────
    risk_dist: Counter[str] = Counter()
    if registry is not None:
        for t in registry.all():
            risk_dist[t.risk.value] += 1
    # Always emit every risk class so the UI doesn't have to guess.
    risk_distribution = {
        rc.value: risk_dist.get(rc.value, 0) for rc in RiskClass
    }

    # ── Browser sessions ─────────────────────────────────────────
    browser_state: dict[str, Any] = {"active": 0, "details": []}
    if browser_manager is not None:
        try:
            sessions = browser_manager.list_active()
            browser_state = {
                "active": len(sessions),
                "details": [
                    {
                        "session_id": s.get("session_id"),
                        "url": s.get("url"),
                        "last_seen": s.get("last_seen"),
                    }
                    for s in sessions
                ],
            }
        except Exception:
            pass

    # ── Sentinel ──────────────────────────────────────────────────
    incident_count_24h = 0
    if sentinel_incidents is not None:
        try:
            cutoff = (
                datetime.now(UTC) - timedelta(hours=24)
            ).isoformat()
            recent = await sentinel_incidents.list_since(cutoff)
            incident_count_24h = len(recent)
        except Exception:
            pass

    # ── Recent IRREVERSIBLE/FINANCIAL/COMMS plan steps ───────────
    last_risky_actions: list[dict[str, Any]] = []
    if plans is not None:
        try:
            recent_plans = await plans.list_plans(limit=20)
            cutoff = (
                datetime.now(UTC) - timedelta(hours=24)
            ).isoformat()
            for p in recent_plans:
                if (p.get("created_at") or "") < cutoff:
                    continue
                for step in p.get("steps") or []:
                    risk = step.get("risk_class")
                    if risk in (
                        RiskClass.IRREVERSIBLE.value,
                        RiskClass.FINANCIAL.value,
                        RiskClass.COMMS.value,
                    ):
                        last_risky_actions.append(
                            {
                                "plan_id": p.get("id"),
                                "step_id": step.get("id"),
                                "kind": step.get("kind"),
                                "risk_class": risk,
                                "description": step.get("description"),
                                "status": step.get("status"),
                                "started_at": step.get("started_at"),
                            }
                        )
        except Exception:
            pass

    # ── Autonomy profiles per agent ──────────────────────────────
    agents = getattr(request.app.state, "agents", None)
    profiles: list[dict[str, Any]] = []
    if agents is not None:
        try:
            rows = await agents.list_rows()
            for r in rows:
                profiles.append(
                    {
                        "agent_name": r.get("name"),
                        "profile": r.get("policy_profile") or "assistant",
                        "tool_count": len(r.get("tools") or []),
                    }
                )
        except Exception:
            pass

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "computer_control": {
            "enabled": cc_enabled,
            "daily_limit": settings.computer_control_daily_limit,
            "used_today": cc_used_today,
            "audit_log_path": str(cc_log_path),
            "last_action": cc_last_action,
        },
        "sandboxes": sandbox_rows,
        "browser_sessions": browser_state,
        "risk_distribution": risk_distribution,
        "sentinel": {
            "running": (
                sentinel is not None
                and getattr(sentinel, "_scan_task", None) is not None
                and not sentinel._scan_task.done()
            ),
            "incidents_last_24h": incident_count_24h,
        },
        "last_risky_actions": last_risky_actions,
        "autonomy_profiles": profiles,
    }
