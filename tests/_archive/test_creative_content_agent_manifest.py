"""Manifest tests for creative_content_agent. Mirrors the
pitch_deck_agent and web_design_agent tests in shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "creative_content_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


# ── Schema / loader ─────────────────────────────────────────────


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "creative_content_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 80


# ── Tool allowlist ──────────────────────────────────────────────


def test_manifest_includes_both_generators(manifest: Manifest) -> None:
    for t in ("nano_banana_generate", "higgsfield_generate"):
        assert t in manifest.tools, f"missing {t}"


def test_manifest_includes_fs_and_net(manifest: Manifest) -> None:
    for t in ("fs_read", "fs_write", "net_fetch"):
        assert t in manifest.tools, f"missing {t}"


def test_manifest_does_not_include_financial_tools(manifest: Manifest) -> None:
    forbidden = {
        "xauusd_place_order",
        "trade_execute",
        "finance_deposit",
        "finance_withdraw",
        "wordpress_push",
    }
    assert not (forbidden & set(manifest.tools))


def test_manifest_does_not_include_agent_create(manifest: Manifest) -> None:
    assert "agent_create" not in manifest.tools


def test_manifest_tools_no_duplicates(manifest: Manifest) -> None:
    assert len(manifest.tools) == len(set(manifest.tools))


def test_manifest_does_not_expose_gmail_directly(manifest: Manifest) -> None:
    """Creative agent has no business sending email — stay out of
    the Gmail surface entirely (no delivery tool either)."""
    for t in manifest.tools:
        assert not t.startswith("gmail_"), f"{t} is a Gmail tool"
    assert "agent_email_deliver" not in manifest.tools


# ── Policy / budget ─────────────────────────────────────────────


def test_manifest_budget_shape(manifest: Manifest) -> None:
    # Video gen is pricey; start conservative.
    assert manifest.policy.budget.per_run_usd == pytest.approx(0.50)
    assert manifest.policy.budget.daily_usd == pytest.approx(5.00)


def test_manifest_per_run_under_daily(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd < manifest.policy.budget.daily_usd


# ── System prompt content ───────────────────────────────────────


def test_system_prompt_non_empty(manifest: Manifest) -> None:
    assert len(manifest.system_prompt.strip()) > 200


def test_system_prompt_distinguishes_image_from_video(
    manifest: Manifest,
) -> None:
    sp = manifest.system_prompt.lower()
    assert "image" in sp
    assert "video" in sp
    # There must be explicit routing to the right tool per kind.
    assert "nano_banana_generate" in sp
    assert "higgsfield_generate" in sp


def test_system_prompt_asks_when_ambiguous(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "ask" in sp


def test_system_prompt_surfaces_tool_errors(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "fail" in sp or "error" in sp


def test_system_prompt_roughly_under_token_budget(manifest: Manifest) -> None:
    assert len(manifest.system_prompt) < 4000


# ── Integrations block (PR #25 panel) ───────────────────────────


def test_manifest_declares_both_api_keys(manifest: Manifest) -> None:
    names = {i.name for i in (manifest.integrations or [])}
    assert "nano_banana_api_key" in names
    assert "higgsfield_api_key" in names


def test_all_integrations_are_api_key_kind(manifest: Manifest) -> None:
    for i in manifest.integrations or []:
        assert i.kind == "api_key", (
            f"{i.name} should be api_key, got {i.kind}"
        )


# ── Sandbox + memory ────────────────────────────────────────────


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


def test_manifest_memory_namespace(manifest: Manifest) -> None:
    assert manifest.memory_namespace == "creative_content"
