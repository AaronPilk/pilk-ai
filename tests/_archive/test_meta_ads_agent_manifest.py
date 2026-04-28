"""Manifest tests for meta_ads_agent. Shape mirrors the
creative_content_agent manifest tests — schema check, tool allowlist,
policy budget, system-prompt contract, integrations, sandbox."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "meta_ads_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


# ── Schema / loader ─────────────────────────────────────────────


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "meta_ads_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 80


# ── Tool allowlist ──────────────────────────────────────────────


def test_manifest_includes_all_meta_ads_tools(manifest: Manifest) -> None:
    required = {
        "meta_ads_list_campaigns",
        "meta_ads_list_adsets",
        "meta_ads_list_ads",
        "meta_ads_get_insights",
        "meta_ads_create_campaign",
        "meta_ads_create_adset",
        "meta_ads_create_ad",
        "meta_ads_create_creative",
        "meta_ads_upload_image",
        "meta_ads_upload_video",
        "meta_ads_set_status",
        "meta_ads_update_budget",
    }
    missing = required - set(manifest.tools)
    assert not missing, f"missing: {missing}"


def test_manifest_includes_fs_read_for_workspace(manifest: Manifest) -> None:
    assert "fs_read" in manifest.tools


def test_manifest_does_not_grant_fs_write(manifest: Manifest) -> None:
    """creative_content_agent owns asset generation — the ads agent
    should never write to the workspace itself."""
    assert "fs_write" not in manifest.tools


def test_manifest_does_not_include_creative_tools(manifest: Manifest) -> None:
    """One agent per domain: the Meta agent never invokes the creative
    generators itself."""
    for t in ("nano_banana_generate", "higgsfield_generate"):
        assert t not in manifest.tools, (
            f"{t} belongs to creative_content_agent"
        )


def test_manifest_does_not_include_agent_create(manifest: Manifest) -> None:
    assert "agent_create" not in manifest.tools


def test_manifest_tools_no_duplicates(manifest: Manifest) -> None:
    assert len(manifest.tools) == len(set(manifest.tools))


def test_manifest_does_not_include_forbidden_financial(
    manifest: Manifest,
) -> None:
    forbidden = {
        "xauusd_place_order",
        "trade_execute",
        "finance_deposit",
        "finance_withdraw",
        "finance_transfer",
        "wordpress_push",
    }
    assert not (forbidden & set(manifest.tools))


# ── Policy / budget ─────────────────────────────────────────────


def test_manifest_budget_shape(manifest: Manifest) -> None:
    # Ad spend isn't metered here (that's Meta's side) — this budget
    # covers PILK's own token spend per run. Still conservative.
    assert manifest.policy.budget.per_run_usd == pytest.approx(1.00)
    assert manifest.policy.budget.daily_usd == pytest.approx(10.00)


def test_manifest_per_run_under_daily(manifest: Manifest) -> None:
    assert (
        manifest.policy.budget.per_run_usd
        < manifest.policy.budget.daily_usd
    )


# ── System prompt content ───────────────────────────────────────


def test_system_prompt_non_empty(manifest: Manifest) -> None:
    assert len(manifest.system_prompt.strip()) > 400


def test_system_prompt_mentions_paused_default(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "paused" in sp


def test_system_prompt_requires_approval_on_activation(
    manifest: Manifest,
) -> None:
    sp = manifest.system_prompt.lower()
    assert "active" in sp
    assert "approval" in sp or "spend" in sp


def test_system_prompt_references_creative_collaboration(
    manifest: Manifest,
) -> None:
    sp = manifest.system_prompt.lower()
    assert "creative_content_agent" in sp or "creative content" in sp


def test_system_prompt_references_insights_loop(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "insight" in sp or "monitor" in sp


def test_system_prompt_roughly_under_token_budget(
    manifest: Manifest,
) -> None:
    assert len(manifest.system_prompt) < 6000


# ── Integrations block ──────────────────────────────────────────


def test_manifest_declares_meta_integrations(manifest: Manifest) -> None:
    names = {i.name for i in (manifest.integrations or [])}
    assert "meta_access_token" in names
    assert "meta_ad_account_id" in names
    assert "meta_page_id" in names


def test_all_integrations_are_api_key_kind(manifest: Manifest) -> None:
    for i in manifest.integrations or []:
        assert i.kind == "api_key", (
            f"{i.name} should be api_key, got {i.kind}"
        )


# ── Sandbox + memory ────────────────────────────────────────────


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


def test_manifest_memory_namespace(manifest: Manifest) -> None:
    assert manifest.memory_namespace == "meta_ads"
