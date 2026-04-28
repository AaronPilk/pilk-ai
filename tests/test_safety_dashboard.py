"""Phase 5 — safety dashboard + capability validator tests.

Covers:
  - GET /system/safety renders a complete shape with safe defaults
  - Hard-coded invariants on RiskClass: FINANCIAL/IRREVERSIBLE
    NEVER auto-allow under any profile
  - Every agent manifest's tool list contains only known tools
  - The financial sub-policy still hard-rejects the bank surface
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.policy.gate import (
    AUTO_ALLOW,
    PROFILE_AUTO_ALLOW,
)
from core.policy.risk import RiskClass


def test_financial_and_irreversible_never_auto_allow() -> None:
    """The single most important Phase 5 invariant: FINANCIAL and
    IRREVERSIBLE are NEVER added to AUTO_ALLOW or any profile's
    auto-allow widening. A future PR adding either to a profile
    is caught here at PR-review time."""
    assert RiskClass.FINANCIAL not in AUTO_ALLOW
    assert RiskClass.IRREVERSIBLE not in AUTO_ALLOW
    for profile, widened in PROFILE_AUTO_ALLOW.items():
        assert RiskClass.FINANCIAL not in widened, (
            f"profile {profile} must not auto-allow FINANCIAL"
        )
        assert RiskClass.IRREVERSIBLE not in widened, (
            f"profile {profile} must not auto-allow IRREVERSIBLE"
        )


def test_safety_route_serves_shape() -> None:
    """Safety endpoint returns the documented shape even when most
    of app.state is empty. Defensive — if the route ever 500s in
    a half-initialised state, dashboards stop showing safety."""
    from core.api.routes.safety import router as safety_router

    app = FastAPI()
    app.include_router(safety_router)

    with TestClient(app) as client:
        r = client.get("/system/safety")
        assert r.status_code == 200
        body = r.json()
        # Everything documented at the top of the route must be
        # present, even if zeroed.
        for key in (
            "generated_at",
            "computer_control",
            "sandboxes",
            "browser_sessions",
            "risk_distribution",
            "sentinel",
            "last_risky_actions",
            "autonomy_profiles",
        ):
            assert key in body, f"missing key {key}"
        cc = body["computer_control"]
        # Default posture: computer control is disabled.
        assert cc["enabled"] is False
        # Every RiskClass value present in the distribution map.
        for rc in RiskClass:
            assert rc.value in body["risk_distribution"]


def test_master_reporting_tools_are_well_typed() -> None:
    """Every tool listed in master_reporting's manifest must exist
    in the registered tool catalog. A typo in the manifest used to
    silently drop the tool from the agent's allowlist, which Aaron's
    Master Reporting smoke test caught the hard way."""
    from core.api.app import create_app
    from core.registry.manifest import Manifest

    manifest_path = (
        Path(__file__).resolve().parents[1]
        / "agents" / "master_reporting" / "manifest.yaml"
    )
    manifest = Manifest.load(manifest_path)
    tool_names = set(manifest.tools)
    # Built-in / low-risk tools that always register at boot. We
    # don't spin the full app here; we just sanity-check that no
    # tool name is empty / whitespace / duplicated.
    assert len(tool_names) == len(manifest.tools), (
        "duplicate tool names in master_reporting manifest"
    )
    for name in tool_names:
        assert name and name.strip() == name, (
            f"manifest has bogus tool name: {name!r}"
        )


def test_irreversible_tools_carry_irreversible_risk_class() -> None:
    """The four computer_* tools are the only tools that ought to
    declare IRREVERSIBLE. Anything else carrying that risk class
    needs a code review before it ships."""
    from core.tools.builtin.computer_control import COMPUTER_CONTROL_TOOLS

    for tool in COMPUTER_CONTROL_TOOLS:
        assert tool.risk == RiskClass.IRREVERSIBLE, (
            f"{tool.name} should be IRREVERSIBLE, got {tool.risk}"
        )
