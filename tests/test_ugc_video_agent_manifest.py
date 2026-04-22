"""Manifest tests for ugc_video_agent.

The agent manifest ships in this PR; the three arcads_* tools it
references (arcads_list_actors, arcads_video_generate,
arcads_video_status) land in a follow-up once the external API spec
is wired against `external-api.arcads.ai/docs`. Tests here lock in
the contract the tools will need to honour, so the follow-up PR
reviewer can see exactly what's expected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "ugc_video_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


# ── Schema / loader ─────────────────────────────────────────────


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "ugc_video_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 150


# ── Tool allowlist ──────────────────────────────────────────────


def test_manifest_declares_arcads_tools(manifest: Manifest) -> None:
    """Three arcads_* tools land in a follow-up PR once the external
    API spec is wired. Names locked in here so the reviewer knows
    what to register."""
    for t in (
        "arcads_list_actors",
        "arcads_video_generate",
        "arcads_video_status",
    ):
        assert t in manifest.tools, f"missing {t}"


def test_manifest_includes_brain_for_grounding(manifest: Manifest) -> None:
    for t in ("brain_search", "brain_note_read", "brain_note_write"):
        assert t in manifest.tools, f"missing {t}"


def test_manifest_includes_memory_tools(manifest: Manifest) -> None:
    assert "memory_remember" in manifest.tools
    assert "memory_list" in manifest.tools


def test_manifest_does_not_include_destructive_surfaces(
    manifest: Manifest,
) -> None:
    forbidden = {
        "agent_create",
        "shell_exec",
        "trade_execute",
        "finance_deposit",
        "finance_withdraw",
        "agent_email_deliver",  # UGC agent doesn't send anything
        "wordpress_push",
        "gmail_send",
    }
    assert not (forbidden & set(manifest.tools))
    for t in manifest.tools:
        assert not t.startswith("gmail_"), f"{t} is a Gmail tool"


def test_manifest_does_not_render_images_directly(manifest: Manifest) -> None:
    """Image / still-frame generation is creative_content_agent's job.
    This agent only drives Arcads."""
    for t in ("nano_banana_generate", "higgsfield_generate"):
        assert t not in manifest.tools, f"{t} belongs to creative_content_agent"


def test_manifest_tools_no_duplicates(manifest: Manifest) -> None:
    assert len(manifest.tools) == len(set(manifest.tools))


# ── Policy / budget ─────────────────────────────────────────────


def test_manifest_budget_shape(manifest: Manifest) -> None:
    # Arcads is ~$11/clip at current pricing. Leave room for one
    # retry + the polling overhead but cap daily so a stuck loop
    # can't run away.
    assert manifest.policy.budget.per_run_usd == pytest.approx(12.00)
    assert manifest.policy.budget.daily_usd == pytest.approx(60.00)


def test_manifest_per_run_under_daily(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd < manifest.policy.budget.daily_usd


def test_manifest_preferred_tier_is_standard(manifest: Manifest) -> None:
    # Routing polling + script validation through LIGHT would end up
    # bouncing back to STANDARD when the actor-filter reasoning runs.
    # Pin directly to skip the churn.
    assert manifest.preferred_tier == "standard"


# ── System prompt content ───────────────────────────────────────


def test_system_prompt_non_empty(manifest: Manifest) -> None:
    assert len(manifest.system_prompt.strip()) > 500


def test_system_prompt_describes_polling_flow(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    # Video generation is async — prompt MUST call out the poll loop
    # or the agent will just fire generate and return before the
    # render completes.
    assert "poll" in sp
    assert "status" in sp


def test_system_prompt_gates_on_script_confirmation(
    manifest: Manifest,
) -> None:
    # Never fire a chargeable render without operator sign-off on
    # the script.
    sp = manifest.system_prompt.lower()
    assert "confirm" in sp or "before firing" in sp


def test_system_prompt_handles_missing_api_key(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "arcads_api_key" in sp
    assert "settings" in sp


def test_system_prompt_roughly_under_token_budget(manifest: Manifest) -> None:
    assert len(manifest.system_prompt) < 4500


# ── Sandbox + memory + integrations ─────────────────────────────


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


def test_manifest_memory_namespace(manifest: Manifest) -> None:
    assert manifest.memory_namespace == "ugc_video"


def test_manifest_declares_arcads_api_key_integration(
    manifest: Manifest,
) -> None:
    names = {i.name for i in (manifest.integrations or [])}
    assert "arcads_api_key" in names


def test_arcads_integration_is_api_key_kind(manifest: Manifest) -> None:
    for i in manifest.integrations or []:
        if i.name == "arcads_api_key":
            assert i.kind == "api_key"
            assert "arcads" in (i.docs_url or "").lower()
            return
    pytest.fail("arcads_api_key integration not declared")
