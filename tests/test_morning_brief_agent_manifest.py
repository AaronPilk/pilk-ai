"""Manifest tests for morning_brief_agent."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "morning_brief_agent"
    / "manifest.yaml"
)

TRIGGER_PATH = (
    Path(__file__).resolve().parents[1]
    / "triggers"
    / "morning_brief"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


# ── Schema / loader ─────────────────────────────────────────────


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "morning_brief_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 120


# ── Tool allowlist ──────────────────────────────────────────────


def test_manifest_includes_required_tools(manifest: Manifest) -> None:
    tools = set(manifest.tools)
    required = {
        "calendar_read_my_today",
        "gmail_search_my_inbox",
        "brain_note_read",
        "brain_note_list",
        "gmail_send_as_pilk",
        "ghl_task_list",
        "brain_note_write",
    }
    missing = required - tools
    assert not missing, f"manifest missing: {sorted(missing)}"


def test_manifest_forbids_unrelated_tools(manifest: Manifest) -> None:
    """Brief composition shouldn't need destructive or agent-spawning
    tools — forbid them defensively so a system-prompt tweak can't
    escalate side effects."""
    forbidden = {
        "fs_write",
        "shell_exec",
        "code_task",
        "agent_create",
    }
    allowed = set(manifest.tools)
    overlap = forbidden & allowed
    assert not overlap, f"unexpected tools in manifest: {sorted(overlap)}"


# ── System prompt + delivery ────────────────────────────────────


def test_system_prompt_specifies_email_delivery(manifest: Manifest) -> None:
    prompt = manifest.system_prompt.lower()
    # Email delivery + target recipient are non-negotiable per spec.
    assert "gmail_send_as_pilk" in prompt
    assert "pilkingtonent@gmail.com" in prompt
    assert "subject" in prompt


def test_preferred_tier_is_standard(manifest: Manifest) -> None:
    """Drafting a five-bullet summary doesn't need Opus."""
    assert manifest.preferred_tier == "standard"


# ── Trigger wiring ──────────────────────────────────────────────


def test_trigger_points_at_new_agent() -> None:
    """morning_brief cron targets morning_brief_agent (email), not the
    legacy Telegram-delivery daily_brief_agent."""
    with TRIGGER_PATH.open() as fh:
        trigger = yaml.safe_load(fh)
    assert trigger["agent_name"] == "morning_brief_agent"
    assert trigger["enabled"] is True
    # 08:00 daily — preserved from the original cron.
    assert trigger["schedule"]["expression"] == "0 8 * * *"
