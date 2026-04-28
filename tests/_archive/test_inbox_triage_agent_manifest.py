"""Manifest tests for inbox_triage_agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "inbox_triage_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "inbox_triage_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 80


def test_manifest_includes_gmail_read_and_send(manifest: Manifest) -> None:
    """Minimum viable triage needs: search, thread read, send.
    If ANY of these are missing the agent can't do its job."""
    tools = set(manifest.tools)
    for required in (
        "gmail_search_my_inbox",
        "gmail_read_me",
        "gmail_thread_read_me",
        "gmail_send_as_me",
    ):
        assert required in tools, f"missing: {required}"


def test_manifest_excludes_system_gmail_send(manifest: Manifest) -> None:
    """Send from the user's account, not the system account. The
    system (gmail_send_as_pilk) is for agent-initiated outreach, not
    personal correspondence the operator will be cc'd on."""
    assert "gmail_send_as_pilk" not in set(manifest.tools)
    assert "agent_email_deliver" not in set(manifest.tools)


def test_manifest_excludes_cross_agent_send_tools(
    manifest: Manifest,
) -> None:
    """Inbox triage is deliberately bounded. Don't let ads, outreach,
    or other send-capable tools sneak in."""
    forbidden = {
        "meta_ads_set_status",
        "google_ads_set_status",
        "slack_send",
        "x_post",
        "x_send_dm",
        "ugc_outreach_log_append",
    }
    leaked = forbidden & set(manifest.tools)
    assert not leaked, f"inbox triage should not carry {leaked}"


def test_manifest_has_budget_caps(manifest: Manifest) -> None:
    budget = manifest.policy.budget
    assert budget.per_run_usd > 0
    assert budget.daily_usd > 0


def test_manifest_declares_user_gmail_oauth(manifest: Manifest) -> None:
    google = next(
        (i for i in manifest.integrations if i.name == "google"), None,
    )
    assert google is not None
    assert google.kind == "oauth"
    assert google.role == "user"
    # Must declare both scopes — read-only + send.
    scopes = set(google.scopes)
    assert "https://www.googleapis.com/auth/gmail.readonly" in scopes
    assert "https://www.googleapis.com/auth/gmail.send" in scopes


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


# ── System-prompt contract ──────────────────────────────────────


def test_prompt_defines_four_buckets(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    for bucket in ("urgent", "reply-now", "file-for-later", "ignore"):
        assert bucket in sp, f"missing bucket: {bucket}"


def test_prompt_requires_log_dedupe(manifest: Manifest) -> None:
    """The triage log is the source of truth for 'already handled'.
    Forgetting to read it means the agent reclassifies every thread
    every run — burns tokens + operator time."""
    sp = manifest.system_prompt or ""
    assert "triage-log.csv" in sp
    assert "FIRST" in sp or "dedupe" in sp.lower()


def test_prompt_forbids_auto_send(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    assert "Never auto-send" in sp or "never auto-send" in sp.lower()


def test_prompt_mentions_read_before_classify(manifest: Manifest) -> None:
    sp = manifest.system_prompt or ""
    assert "Read before classifying" in sp or "read before" in sp.lower()
