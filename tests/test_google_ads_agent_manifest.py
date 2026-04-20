"""Manifest tests for google_ads_agent. Mirrors meta_ads_agent
manifest tests — schema check, tool allowlist, policy budget,
system-prompt contract, integrations, sandbox."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "google_ads_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "google_ads_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 80


def test_manifest_includes_every_google_ads_tool(manifest: Manifest) -> None:
    required = {
        "google_ads_list_campaigns",
        "google_ads_list_ad_groups",
        "google_ads_list_ads",
        "google_ads_get_metrics",
        "google_ads_run_gaql",
        "google_ads_create_budget",
        "google_ads_create_campaign",
        "google_ads_create_ad_group",
        "google_ads_add_keywords",
        "google_ads_add_negative_keywords",
        "google_ads_create_responsive_search_ad",
        "google_ads_set_status",
        "google_ads_update_budget",
    }
    missing = required - set(manifest.tools)
    assert not missing, f"missing: {missing}"


def test_manifest_excludes_meta_tools(manifest: Manifest) -> None:
    """Hard-gate: the Google Ads agent must NOT have Meta tools. One
    agent per domain keeps the narrow-scope / cost-efficiency
    invariant."""
    meta_leaked = [t for t in manifest.tools if t.startswith("meta_ads_")]
    assert not meta_leaked, f"Meta tools in google_ads_agent: {meta_leaked}"


def test_manifest_has_budget_caps(manifest: Manifest) -> None:
    budget = manifest.policy.budget
    assert budget.per_run_usd > 0
    assert budget.daily_usd > 0


def test_manifest_declares_all_five_required_secrets(
    manifest: Manifest,
) -> None:
    names = {i.name for i in manifest.integrations}
    for required in (
        "google_ads_developer_token",
        "google_ads_client_id",
        "google_ads_client_secret",
        "google_ads_refresh_token",
        "google_ads_customer_id",
    ):
        assert required in names, f"integrations missing: {required}"


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


# ── System-prompt contract ──────────────────────────────────────


def test_prompt_enforces_paused_by_default(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    assert "PAUSED" in sp
    assert "ENABLED is FINANCIAL" in sp or "status=ENABLED" in sp


def test_prompt_forbids_silent_retries(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    # Word "retry" should appear in a "don't retry" context.
    assert "Never retry" in sp or "don't retry" in sp.lower() or "do not retry" in sp.lower()


def test_prompt_mentions_hand_off_to_creative(manifest: Manifest) -> None:
    """Composability principle — cross-domain work routes through
    PILK, not direct tool calls across agent boundaries."""
    sp = manifest.system_prompt or ""
    assert "creative_content_agent" in sp


def test_prompt_flags_missing_keys_behaviour(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    assert "not configured" in sp.lower() or "Missing keys" in sp
