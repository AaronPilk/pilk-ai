"""Manifest tests for copy_agent. Mirrors the creative_content_agent
and prospector_agent tests in shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "copy_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


# ── Schema / loader ─────────────────────────────────────────────


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "copy_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 120


# ── Tool allowlist ──────────────────────────────────────────────


def test_manifest_includes_brain_and_memory_reads(manifest: Manifest) -> None:
    for t in ("brain_search", "brain_note_read", "memory_list"):
        assert t in manifest.tools, f"missing {t}"


def test_manifest_includes_llm_ask_for_self_grading(manifest: Manifest) -> None:
    assert "llm_ask" in manifest.tools


def test_manifest_includes_memory_remember_for_patterns(
    manifest: Manifest,
) -> None:
    assert "memory_remember" in manifest.tools


def test_manifest_does_not_expose_delivery_surfaces(manifest: Manifest) -> None:
    """Copy agent drafts; delivery is the operator's call. No Gmail,
    no Meta / Google Ads write tools, no wordpress_push."""
    forbidden = {
        "agent_email_deliver",
        "wordpress_push",
        "meta_ads_create",
        "google_ads_create",
        "gmail_send",
    }
    assert not (forbidden & set(manifest.tools))
    for t in manifest.tools:
        assert not t.startswith("gmail_"), f"{t} is a Gmail tool"


def test_manifest_does_not_include_destructive_surfaces(
    manifest: Manifest,
) -> None:
    forbidden = {
        "agent_create",
        "shell_exec",
        "trade_execute",
        "finance_deposit",
        "finance_withdraw",
        "nano_banana_generate",
        "higgsfield_generate",
    }
    assert not (forbidden & set(manifest.tools))


def test_manifest_tools_no_duplicates(manifest: Manifest) -> None:
    assert len(manifest.tools) == len(set(manifest.tools))


# ── Policy / budget ─────────────────────────────────────────────


def test_manifest_budget_shape(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd == pytest.approx(0.25)
    assert manifest.policy.budget.daily_usd == pytest.approx(3.00)


def test_manifest_per_run_under_daily(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd < manifest.policy.budget.daily_usd


def test_manifest_preferred_tier_is_standard(manifest: Manifest) -> None:
    # Copy is a reasoning-dense task — Haiku/LIGHT would produce flat
    # output. Pin to STANDARD so every run hits Sonnet.
    assert manifest.preferred_tier == "standard"


# ── System prompt content ───────────────────────────────────────


def test_system_prompt_non_empty(manifest: Manifest) -> None:
    assert len(manifest.system_prompt.strip()) > 400


def test_system_prompt_enumerates_channels(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    for channel in ("ad", "landing", "email"):
        assert channel in sp, f"missing channel: {channel}"


def test_system_prompt_calls_out_character_caps(manifest: Manifest) -> None:
    # Ad and email copy live and die by character limits. The prompt
    # must tell the agent to enforce them.
    sp = manifest.system_prompt.lower()
    assert "chars" in sp or "character" in sp


def test_system_prompt_forbids_fabrication(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "fabricate" in sp or "[proof needed]" in sp or "don't guess" in sp


def test_system_prompt_explains_handoff(manifest: Manifest) -> None:
    # Copy agent does NOT send anything — the prompt should be
    # explicit that delivery is the operator's call.
    sp = manifest.system_prompt.lower()
    assert "draft" in sp
    assert "send" in sp  # ...never send / don't send / operator's call


def test_system_prompt_roughly_under_token_budget(manifest: Manifest) -> None:
    assert len(manifest.system_prompt) < 4000


# ── Sandbox + memory ────────────────────────────────────────────


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


def test_manifest_memory_namespace(manifest: Manifest) -> None:
    assert manifest.memory_namespace == "copy"
