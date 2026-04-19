"""Manifest shape tests for web_design_agent.

Verifies the YAML parses via the PILK manifest loader, the tool list
covers what the spec prescribes, budget caps are sensible, and the
system prompt enforces the hard-rule language the agent relies on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "web_design_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


# ── Schema / loader ─────────────────────────────────────────────


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "web_design_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    # A one-line description defeats the purpose of the field when the
    # orchestrator uses it to route requests. Require more than a
    # filler sentence.
    assert len(manifest.description) >= 80


# ── Tool allowlist ──────────────────────────────────────────────


def test_manifest_tools_include_html_export(manifest: Manifest) -> None:
    assert "html_export" in manifest.tools


def test_manifest_tools_include_fs_and_net(manifest: Manifest) -> None:
    for t in ("fs_read", "fs_write", "net_fetch"):
        assert t in manifest.tools, f"missing {t}"


def test_manifest_tools_include_browser_surface(manifest: Manifest) -> None:
    # For reference-site screenshots / DOM inspection.
    for t in (
        "browser_session_open",
        "browser_navigate",
        "browser_session_close",
    ):
        assert t in manifest.tools, f"missing {t}"


def test_manifest_does_not_include_financial_tools(manifest: Manifest) -> None:
    forbidden = {
        "xauusd_place_order",
        "xauusd_take_over",
        "xauusd_flatten_all",
        "wordpress_push",  # PR D adds this via a follow-up commit
        "trade_execute",
        "finance_deposit",
        "finance_withdraw",
        "finance_transfer",
    }
    assert not (forbidden & set(manifest.tools))


def test_manifest_does_not_include_agent_create(manifest: Manifest) -> None:
    assert "agent_create" not in manifest.tools


def test_manifest_tools_no_duplicates(manifest: Manifest) -> None:
    assert len(manifest.tools) == len(set(manifest.tools))


# ── Policy / budget ─────────────────────────────────────────────


def test_manifest_budget_shape(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd == pytest.approx(0.50)
    assert manifest.policy.budget.daily_usd == pytest.approx(5.00)


def test_manifest_per_run_under_daily(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd < manifest.policy.budget.daily_usd


# ── System prompt content ───────────────────────────────────────


def test_system_prompt_non_empty(manifest: Manifest) -> None:
    assert len(manifest.system_prompt.strip()) > 200


def test_system_prompt_enforces_html_export_only(manifest: Manifest) -> None:
    """The agent is explicitly told NOT to emit HTML by hand. Loss of
    this rule is a regression worth catching."""
    sp = manifest.system_prompt.lower()
    # Any phrasing is fine — check for the two-keyword union.
    assert "html_export" in sp
    assert "by hand" in sp or "hand-write" in sp or "never" in sp


def test_system_prompt_requires_alt_text(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "alt" in sp
    assert "image" in sp


def test_system_prompt_references_client_lookup(manifest: Manifest) -> None:
    """When the user names a client, the agent must consult
    clients/<slug>.yaml — not guess."""
    sp = manifest.system_prompt.lower()
    assert "client" in sp
    assert "clients/" in sp or "clients\\/" in sp
    assert "ask" in sp  # must ask before guessing a brand voice


def test_system_prompt_defers_elementor(manifest: Manifest) -> None:
    """Elementor conversion is another agent's job."""
    sp = manifest.system_prompt.lower()
    assert "elementor_converter_agent" in sp


def test_system_prompt_roughly_under_token_budget(manifest: Manifest) -> None:
    """Coarse: one token per ~4 chars. 3000 chars ≈ 750 tokens.
    Anything over this is a prompt-design smell and costs money per
    turn."""
    assert len(manifest.system_prompt) < 3000


# ── Sandbox + memory ────────────────────────────────────────────


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


def test_manifest_memory_namespace(manifest: Manifest) -> None:
    assert manifest.memory_namespace == "web_design_agent"
