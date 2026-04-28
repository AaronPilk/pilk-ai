"""Manifest tests for creative_agent (creative direction, not
rendering — that's creative_content_agent's job)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "creative_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


# ── Schema / loader ─────────────────────────────────────────────


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "creative_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 150


# ── Tool allowlist ──────────────────────────────────────────────


def test_manifest_includes_brain_for_grounding(manifest: Manifest) -> None:
    for t in ("brain_search", "brain_note_read", "brain_note_write"):
        assert t in manifest.tools, f"missing {t}"


def test_manifest_does_not_render_assets(manifest: Manifest) -> None:
    """Creative agent PLANS — rendering is creative_content_agent's job.
    The two exist separately so plans can be reviewed and reused
    without burning generation credits."""
    rendering = {"nano_banana_generate", "higgsfield_generate"}
    assert not (rendering & set(manifest.tools))


def test_manifest_does_not_write_final_copy(manifest: Manifest) -> None:
    """Copy generation is copy_agent's job; this agent only proposes
    headline direction, not variants. llm_ask would enable it to
    generate copy itself — hold the line."""
    assert "llm_ask" not in manifest.tools


def test_manifest_does_not_expose_delivery_surfaces(manifest: Manifest) -> None:
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
    }
    assert not (forbidden & set(manifest.tools))


def test_manifest_tools_no_duplicates(manifest: Manifest) -> None:
    assert len(manifest.tools) == len(set(manifest.tools))


def test_manifest_includes_memory_remember(manifest: Manifest) -> None:
    assert "memory_remember" in manifest.tools


# ── Policy / budget ─────────────────────────────────────────────


def test_manifest_budget_shape(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd == pytest.approx(0.20)
    assert manifest.policy.budget.daily_usd == pytest.approx(2.00)


def test_manifest_per_run_under_daily(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd < manifest.policy.budget.daily_usd


def test_manifest_preferred_tier_is_standard(manifest: Manifest) -> None:
    assert manifest.preferred_tier == "standard"


# ── System prompt content ───────────────────────────────────────


def test_system_prompt_non_empty(manifest: Manifest) -> None:
    assert len(manifest.system_prompt.strip()) > 400


def test_system_prompt_forbids_rendering_and_final_copy(
    manifest: Manifest,
) -> None:
    """The whole point of this agent vs creative_content_agent is the
    split. The system prompt should say it explicitly so the LLM
    doesn't overreach on a tool the manifest happens to expose."""
    sp = manifest.system_prompt.lower()
    assert "not render" in sp or "do not render" in sp or "not generate" in sp


def test_system_prompt_requires_brain_grounding(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "brain_search" in sp
    assert "brand" in sp or "brief" in sp


def test_system_prompt_caps_concept_count(manifest: Manifest) -> None:
    # Direction should stay tight — 2-3 concepts max.
    sp = manifest.system_prompt.lower()
    assert "2" in sp and "3" in sp


def test_system_prompt_names_handoff_agents(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "creative_content_agent" in sp
    assert "copy_agent" in sp


def test_system_prompt_roughly_under_token_budget(manifest: Manifest) -> None:
    assert len(manifest.system_prompt) < 4000


# ── Sandbox + memory ────────────────────────────────────────────


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


def test_manifest_memory_namespace(manifest: Manifest) -> None:
    assert manifest.memory_namespace == "creative"
