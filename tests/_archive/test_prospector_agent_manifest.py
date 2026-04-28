"""Manifest tests for prospector_agent. Deliberately tighter than the
full sales_ops_agent — the prospector's one job is "hand me a sheet
of leads", and we want the manifest to match that scope exactly."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "prospector_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "prospector_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 80


def test_manifest_is_pinned_to_light_tier(manifest: Manifest) -> None:
    """The whole point of this agent is "cheap and capable" — a Sonnet
    planner turn on every tool step would undo that. The manifest
    must pin to LIGHT so the governor routes planner turns through the
    subscription-backed Claude Code CLI provider."""
    assert manifest.preferred_tier == "light", (
        "prospector_agent must pin preferred_tier to 'light' — "
        "the cheap routing is load-bearing for the agent's value "
        "prop"
    )


def test_manifest_has_required_pipeline_tools(manifest: Manifest) -> None:
    """The prospector's fixed pipeline: places → audit → hunter →
    sheet → email. Every tool in that chain must be on the allowlist
    or the agent can't complete its job."""
    required = {
        "google_places_search",
        "site_audit",
        "hunter_find_email",
        "sheets_create",
        "sheets_append_rows",
        "agent_email_deliver",
    }
    missing = required - set(manifest.tools)
    assert not missing, f"missing: {missing}"


def test_manifest_excludes_outreach_tools(manifest: Manifest) -> None:
    """Prospector is discovery-only — actual outreach belongs in
    sales_ops_agent. Block raw outbound surfaces from leaking in."""
    outreach_markers = {
        "gmail_send_as_me",
        "slack_send",
        "x_post",
        "x_send_dm",
        "browser_form_fill",
        "ghl_contact_create",
        "ghl_contact_add_note",
    }
    leaked = outreach_markers & set(manifest.tools)
    assert not leaked, f"prospector shouldn't have outreach tools: {leaked}"


def test_manifest_has_tight_budget(manifest: Manifest) -> None:
    """LIGHT-tier runs should cost ~$0 in LLM spend; a per-run cap
    above $1 signals somebody forgot the tier pin or the agent drifted
    into Sonnet territory."""
    budget = manifest.policy.budget
    assert 0 < budget.per_run_usd <= 1.0, (
        f"per_run_usd {budget.per_run_usd} too high for a LIGHT-tier "
        "agent"
    )


def test_manifest_declares_google_oauth_with_sheets_scopes(
    manifest: Manifest,
) -> None:
    google = next(
        (i for i in manifest.integrations if i.name == "google"), None,
    )
    assert google is not None, "prospector needs Google OAuth"
    assert google.role == "user"
    joined = " ".join(google.scopes)
    assert "spreadsheets" in joined, (
        "prospector must declare the spreadsheets scope so the "
        "Expand-access UI surfaces it"
    )
    assert "gmail.send" in joined, (
        "prospector must declare gmail.send so email delivery works"
    )


def test_manifest_declares_api_key_integrations(
    manifest: Manifest,
) -> None:
    api_keys = {i.name for i in manifest.integrations if i.kind == "api_key"}
    assert "google_places_api_key" in api_keys
    assert "pagespeed_api_key" in api_keys
    assert "hunter_io_api_key" in api_keys


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


# ── System-prompt contract ──────────────────────────────────────


def test_prompt_describes_full_pipeline(manifest: Manifest) -> None:
    """The five pipeline steps must be mentioned in the system prompt
    so the LLM has a clear map. If the prompt drifts away from the
    playbook, drop the test — don't paper it over."""
    sp = (manifest.system_prompt or "").lower()
    for marker in (
        "google_places_search",
        "site_audit",
        "hunter_find_email",
        "sheets_create",
        "sheets_append_rows",
        "agent_email_deliver",
    ):
        assert marker.lower() in sp, f"prompt missing mention of {marker}"


def test_prompt_requires_single_batched_append(manifest: Manifest) -> None:
    """A tight invariant: LLMs love to loop one tool call per row.
    The prompt must explicitly forbid that pattern so we don't waste
    quota + planner turns."""
    sp = (manifest.system_prompt or "").lower()
    # "ONE call" / "single batch" / "do NOT loop" style markers.
    assert (
        "one call" in sp
        or "one batch" in sp
        or "single call" in sp
        or "do not loop" in sp
        or "don't loop" in sp
    ), "prompt must forbid per-row append loops"


def test_prompt_forbids_sending_outreach(manifest: Manifest) -> None:
    """Scope guard — prospector may *draft* personalised cold outreach
    for the top-ranked leads (via ``gmail_draft_save_as_me``), but it
    must never SEND. If the prompt starts mentioning
    ``gmail_send_as_me`` / form-fill / real sends, the agent is
    bleeding into sales_ops_agent's territory."""
    sp = (manifest.system_prompt or "").lower()
    assert (
        "drafts only" in sp
        or "do not call gmail_send_as_me" in sp
        or "never call gmail_send_as_me" in sp
    ), (
        "prompt must explicitly forbid calls to gmail_send_as_me / "
        "any real send — drafts only"
    )
