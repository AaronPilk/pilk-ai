"""Manifest tests for ads_audit_agent — the wrap-a-skill template
agent. Verifies the hard guarantees: no mutation tools, explicit
code_task delegation, report goes to workspace/audits/."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "ads_audit_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "ads_audit_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 80


def test_manifest_tools_include_code_task_and_fs_read(
    manifest: Manifest,
) -> None:
    """These two are the skeleton of every wrap-a-skill agent:
    code_task dispatches the skill, fs_read pulls the output back."""
    tools = set(manifest.tools)
    assert "code_task" in tools
    assert "fs_read" in tools


def test_manifest_declares_read_only_platform_tools(
    manifest: Manifest,
) -> None:
    """We allow the platform list/read tools (so the agent can sanity-
    check connectivity before delegating) but NOT any mutation tool."""
    tools = set(manifest.tools)
    assert "meta_ads_list_campaigns" in tools
    assert "google_ads_list_campaigns" in tools


def test_manifest_excludes_every_mutation_tool(manifest: Manifest) -> None:
    """Hard gate: an auditor must never mutate campaigns. If ANY of
    these slip in, the agent loses its read-only invariant and the
    approval story for spend gets muddled."""
    forbidden = {
        # Meta mutations
        "meta_ads_create_campaign",
        "meta_ads_create_adset",
        "meta_ads_create_ad",
        "meta_ads_create_creative",
        "meta_ads_upload_image",
        "meta_ads_upload_video",
        "meta_ads_set_status",
        "meta_ads_update_budget",
        # Google mutations
        "google_ads_create_budget",
        "google_ads_create_campaign",
        "google_ads_create_ad_group",
        "google_ads_add_keywords",
        "google_ads_add_negative_keywords",
        "google_ads_create_responsive_search_ad",
        "google_ads_set_status",
        "google_ads_update_budget",
        # Write-elsewhere
        "fs_write",
        "shell_exec",
    }
    leaked = forbidden & set(manifest.tools)
    assert not leaked, f"ads_audit_agent should not mutate: {leaked}"


def test_manifest_has_budget_caps(manifest: Manifest) -> None:
    budget = manifest.policy.budget
    assert budget.per_run_usd > 0
    assert budget.daily_usd > 0


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


# ── System-prompt contract ──────────────────────────────────────


def test_prompt_names_the_wrapped_skill(manifest: Manifest) -> None:
    """Whole point of this agent is wrapping `claude-ads`. If that
    identifier drifts out of the prompt we've broken the contract."""
    sp = manifest.system_prompt or ""
    assert "claude-ads" in sp


def test_prompt_forbids_mutations(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    assert "No mutations" in sp or "no mutations" in sp.lower()


def test_prompt_routes_report_to_audits_dir(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    assert "workspace/audits/" in sp or "audits/" in sp


def test_prompt_references_code_task_for_delegation(
    manifest: Manifest,
) -> None:
    sp = manifest.system_prompt or ""
    assert "code_task" in sp


def test_prompt_routes_mutations_to_platform_agents(
    manifest: Manifest,
) -> None:
    sp = manifest.system_prompt or ""
    assert "meta_ads_agent" in sp
    assert "google_ads_agent" in sp


def test_prompt_demands_top_three_fixes(manifest: Manifest) -> None:
    """Operator-facing summary contract: don't dump all 87 findings,
    surface the three highest-leverage fixes."""
    sp = manifest.system_prompt or ""
    assert "top-3" in sp.lower() or "top 3" in sp.lower() or "Top 3" in sp
