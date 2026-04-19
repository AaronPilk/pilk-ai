"""Manifest tests for elementor_converter_agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "elementor_converter_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "elementor_converter_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 80


# ── Tool allowlist ──────────────────────────────────────────────


def test_manifest_tools_narrow(manifest: Manifest) -> None:
    """This agent does ONE thing — convert IR/HTML → Elementor JSON.
    The tool allowlist should stay tight to keep the planner focused.
    """
    assert set(manifest.tools) == {"fs_read", "fs_write", "elementor_validate"}


def test_manifest_does_not_include_wordpress_push(manifest: Manifest) -> None:
    """Pushing to WordPress is a separate step — this agent stops at
    writing the validated JSON to disk."""
    assert "wordpress_push" not in manifest.tools


def test_manifest_does_not_include_html_export(manifest: Manifest) -> None:
    """This agent CONSUMES IR/HTML output; it doesn't produce either.
    Giving it html_export would muddle the division of labor."""
    assert "html_export" not in manifest.tools


def test_manifest_does_not_include_delivery_or_finance(manifest: Manifest) -> None:
    forbidden = {
        "agent_email_deliver",
        "slides_create",
        "finance_deposit",
        "finance_withdraw",
        "trade_execute",
    }
    assert not (forbidden & set(manifest.tools))


def test_manifest_does_not_include_agent_create(manifest: Manifest) -> None:
    assert "agent_create" not in manifest.tools


# ── Budget ─────────────────────────────────────────────────────


def test_manifest_budget_shape(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd == pytest.approx(0.30)
    assert manifest.policy.budget.daily_usd == pytest.approx(3.00)


def test_manifest_per_run_under_daily(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd < manifest.policy.budget.daily_usd


# ── System prompt content ───────────────────────────────────────


def test_system_prompt_mandates_validation_before_write(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "elementor_validate" in sp
    assert "before" in sp or "never" in sp
    # The validate-patch loop must be present.
    assert "patch" in sp or "re-validate" in sp or "iterate" in sp


def test_system_prompt_mentions_export_shape(manifest: Manifest) -> None:
    """The agent must know the top-level keys Elementor expects."""
    sp = manifest.system_prompt
    for key in ("version", "title", "type", "content", "page_settings"):
        assert key in sp, f"system prompt missing reference to {key!r}"


def test_system_prompt_widget_mapping_present(manifest: Manifest) -> None:
    """Widget type mapping is where the LLM is most likely to hallucinate
    — pin it into the prompt and check for a few entries."""
    sp = manifest.system_prompt
    assert "heading" in sp
    assert "text-editor" in sp
    assert "widgetType" in sp


def test_system_prompt_mentions_isinner_rule(manifest: Manifest) -> None:
    sp = manifest.system_prompt
    assert "isInner" in sp
    # Both cases: top-level false, nested true.
    assert "false" in sp.lower()
    assert "true" in sp.lower()


def test_system_prompt_roughly_under_token_budget(manifest: Manifest) -> None:
    # Tighter than the design agents because this prompt is
    # primarily scaffolding + widget-type mapping, not open-ended
    # planning.
    assert len(manifest.system_prompt) < 4000


# ── Sandbox + memory ────────────────────────────────────────────


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


def test_manifest_memory_namespace(manifest: Manifest) -> None:
    assert manifest.memory_namespace == "elementor_converter_agent"
