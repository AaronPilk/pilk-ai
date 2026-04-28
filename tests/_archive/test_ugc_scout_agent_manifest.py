"""Manifest tests for ugc_scout_agent. Mirrors the meta_ads_agent
manifest tests — schema check, tool allowlist, budget, integrations,
sandbox, and a couple of rubric guardrails on the system prompt."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "ugc_scout_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "ugc_scout_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 80


def test_manifest_includes_every_ugc_tool(manifest: Manifest) -> None:
    required = {
        "ugc_instagram_hashtag_search",
        "ugc_instagram_profile",
        "ugc_tiktok_hashtag_search",
        "ugc_tiktok_profile",
        "ugc_find_email",
        "ugc_export_csv",
    }
    missing = required - set(manifest.tools)
    assert not missing, f"missing: {missing}"


def test_manifest_excludes_outreach_tools(manifest: Manifest) -> None:
    """V1 is discovery-only. Outreach tools land in a separate agent —
    if any outreach surface leaks in here, the approval story gets
    muddled. Hard-gate that at the manifest level."""
    outreach_markers = {
        "gmail_send",
        "email_send",
        "slack_send",
        "x_post",
        "x_send_dm",
        "meta_ads_set_status",
    }
    leaked = outreach_markers & set(manifest.tools)
    assert not leaked, (
        f"ugc_scout_agent should not have outreach tools: {leaked}"
    )


def test_manifest_has_budget_caps(manifest: Manifest) -> None:
    budget = manifest.policy.budget
    assert budget.per_run_usd > 0
    assert budget.daily_usd > 0


def test_manifest_declares_apify_integration(manifest: Manifest) -> None:
    names = {i.name for i in manifest.integrations}
    assert "apify_api_token" in names
    # hunter is optional but should be declared so the UI shows its
    # status.
    assert "hunter_io_api_key" in names


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


# ── System-prompt contract ──────────────────────────────────────


def test_prompt_mentions_all_five_rubric_axes(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    for axis in (
        "score_quality",
        "score_brand_fit",
        "score_business_utility",
        "score_virality",
        "score_cringe_risk",
    ):
        assert axis in sp, f"system prompt missing rubric axis: {axis}"


def test_prompt_weights_business_utility_heaviest(manifest: Manifest) -> None:
    """The operator's hard constraint: this agent should distinguish
    content that *converts to sales* from slop. The rubric weights
    encode that; the system prompt must actually state it so the LLM
    scores correspond to the spec."""
    sp = manifest.system_prompt or ""
    assert "0.30" in sp or "0.3" in sp, (
        "business_utility weight (0.30) should appear in the prompt"
    )


def test_prompt_names_the_downstream_outreach_agent(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    assert "outreach" in sp.lower()


def test_prompt_flags_missing_keys_behaviour(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    # The agent must not retry when Apify is not configured — that
    # would burn the operator's time and never succeed.
    assert "Missing keys" in sp or "not configured" in sp.lower()
