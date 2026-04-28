"""Manifest tests for print_design_agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "print_design_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "print_design_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 80


def test_manifest_includes_every_print_tool(manifest: Manifest) -> None:
    required = {
        "print_design_flyer",
        "print_design_business_card",
        "print_design_banner",
        "print_design_list_templates",
    }
    missing = required - set(manifest.tools)
    assert not missing, f"missing: {missing}"


def test_manifest_has_budget_caps(manifest: Manifest) -> None:
    budget = manifest.policy.budget
    assert budget.per_run_usd > 0
    assert budget.daily_usd > 0


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


# ── System-prompt contract ──────────────────────────────────────


def test_prompt_mentions_bleed_and_crop_marks(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    assert "bleed" in sp.lower()
    assert "crop" in sp.lower()


def test_prompt_routes_imagery_to_creative(manifest: Manifest) -> None:
    """Composability boundary — the print agent never renders its own
    imagery; it reads what creative_content_agent wrote to the
    workspace."""
    sp = manifest.system_prompt or ""
    assert "creative_content_agent" in sp


def test_prompt_says_rgb_is_fine(manifest: Manifest) -> None:
    """The whole V1 premise is 'RGB 300 DPI is good enough for every
    modern print shop'. If the system prompt silently suggests CMYK,
    we're misleading the operator about what the tool does."""
    sp = manifest.system_prompt or ""
    assert "RGB" in sp
