"""Manifest tests for ugc_outreach_agent. Mirrors ugc_scout_agent
manifest tests — schema, tool allowlist, hard-gates against scope
creep, policy budget, and system-prompt contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "ugc_outreach_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "ugc_outreach_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 80


def test_manifest_includes_outreach_helpers(manifest: Manifest) -> None:
    required = {
        "ugc_read_shortlist",
        "ugc_outreach_log_read",
        "ugc_outreach_log_append",
        "agent_email_deliver",
    }
    missing = required - set(manifest.tools)
    assert not missing, f"missing: {missing}"


def test_manifest_excludes_scout_discovery_tools(manifest: Manifest) -> None:
    """Outreach must NOT carry discovery tools — otherwise the loop
    can invent creators outside the scout's rubric. The whole point
    of the two-agent split is the rubric gate."""
    forbidden = {
        "ugc_instagram_hashtag_search",
        "ugc_instagram_profile",
        "ugc_tiktok_hashtag_search",
        "ugc_tiktok_profile",
        "ugc_find_email",
        "ugc_export_csv",
    }
    leaked = forbidden & set(manifest.tools)
    assert not leaked, f"discovery tool leaked into outreach: {leaked}"


def test_manifest_excludes_user_inbox_send(manifest: Manifest) -> None:
    """Never send from the operator's personal Gmail — only from the
    system account via agent_email_deliver."""
    personal_send = {"gmail_send_as_me", "gmail_send"}
    leaked = personal_send & set(manifest.tools)
    assert not leaked


def test_manifest_has_budget_caps(manifest: Manifest) -> None:
    budget = manifest.policy.budget
    assert budget.per_run_usd > 0
    assert budget.daily_usd > 0


def test_manifest_declares_google_system_oauth(manifest: Manifest) -> None:
    google = next(
        (i for i in manifest.integrations if i.name == "google"), None,
    )
    assert google is not None, "manifest should declare the Google OAuth"
    assert google.kind == "oauth"
    assert google.role == "system"
    assert "https://www.googleapis.com/auth/gmail.send" in google.scopes


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


# ── system-prompt contract ──────────────────────────────────────


def test_prompt_enforces_approval_gating(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    assert "approval" in sp.lower() or "approved" in sp.lower()


def test_prompt_requires_dedupe_before_send(manifest: Manifest) -> None:
    """The system prompt must require the log-read step BEFORE any
    iterating — doing this right is what prevents double-sends."""
    sp = manifest.system_prompt or ""
    assert "ugc_outreach_log_read" in sp
    assert "FIRST" in sp or "first" in sp


def test_prompt_forbids_templates(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    # The word "template" should appear in a negative context.
    assert "No templates" in sp or "not a template" in sp.lower()


def test_prompt_caps_body_length(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    # Hard wordcap keeps cold emails scannable.
    assert "180 words" in sp or "180" in sp


def test_prompt_requires_one_approval_per_send(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    assert "One send per approval" in sp or "one send per approval" in sp.lower()


def test_prompt_points_downstream_collaborators(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    assert "scout" in sp.lower()
