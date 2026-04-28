"""Manifest tests for pitch_deck_agent. Mirrors the web_design_agent
tests in shape so future additions are cookie-cutter."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.registry.manifest import Manifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "pitch_deck_agent"
    / "manifest.yaml"
)


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.load(MANIFEST_PATH)


# ── Schema / loader ─────────────────────────────────────────────


def test_manifest_parses(manifest: Manifest) -> None:
    assert manifest.name == "pitch_deck_agent"
    assert manifest.version == "0.1.0"


def test_manifest_description_is_substantive(manifest: Manifest) -> None:
    assert len(manifest.description) >= 80


# ── Tool allowlist ──────────────────────────────────────────────


def test_manifest_tools_include_slides_and_delivery(manifest: Manifest) -> None:
    for t in ("slides_create", "agent_email_deliver"):
        assert t in manifest.tools, f"missing {t}"


def test_manifest_tools_include_fs_and_net(manifest: Manifest) -> None:
    for t in ("fs_read", "fs_write", "net_fetch"):
        assert t in manifest.tools, f"missing {t}"


def test_manifest_does_not_include_canva_yet(manifest: Manifest) -> None:
    # Canva integration is a follow-up PR; shouldn't be referenced
    # here until that tool exists in the registry.
    forbidden = {
        "canva_generate_presentation",
        "canva_generate_design",
        "canva_export_design",
        "canva_list_brand_kits",
    }
    assert not (forbidden & set(manifest.tools))


def test_manifest_does_not_include_financial_tools(manifest: Manifest) -> None:
    forbidden = {
        "xauusd_place_order",
        "xauusd_take_over",
        "xauusd_flatten_all",
        "wordpress_push",
        "trade_execute",
        "finance_deposit",
        "finance_withdraw",
    }
    assert not (forbidden & set(manifest.tools))


def test_manifest_does_not_include_agent_create(manifest: Manifest) -> None:
    assert "agent_create" not in manifest.tools


def test_manifest_tools_no_duplicates(manifest: Manifest) -> None:
    assert len(manifest.tools) == len(set(manifest.tools))


def test_manifest_does_not_expose_gmail_directly(manifest: Manifest) -> None:
    """Agents never call gmail_send_as_* directly — they route through
    agent_email_deliver so the subject format stays consistent and the
    'system' account is used."""
    for t in manifest.tools:
        assert not t.startswith("gmail_send_"), (
            f"{t} is a direct Gmail call — use agent_email_deliver instead"
        )


# ── Policy / budget ─────────────────────────────────────────────


def test_manifest_budget_shape(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd == pytest.approx(0.75)
    assert manifest.policy.budget.daily_usd == pytest.approx(5.00)


def test_manifest_per_run_under_daily(manifest: Manifest) -> None:
    assert manifest.policy.budget.per_run_usd < manifest.policy.budget.daily_usd


# ── System prompt content ───────────────────────────────────────


def test_system_prompt_non_empty(manifest: Manifest) -> None:
    assert len(manifest.system_prompt.strip()) > 200


def test_system_prompt_requires_outline_first(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "outline" in sp
    assert "before" in sp or "first" in sp
    assert "slides_create" in sp


def test_system_prompt_mandates_speaker_notes(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "speaker_notes" in sp or "speaker notes" in sp


def test_system_prompt_references_client_lookup(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "client" in sp
    assert "clients/" in sp or "clients\\/" in sp


def test_system_prompt_never_sends_without_ask(manifest: Manifest) -> None:
    """Users have to explicitly ask before the agent delivers —
    don't slide a deck into someone's inbox unprompted."""
    sp = manifest.system_prompt.lower()
    assert "agent_email_deliver" in sp
    assert "asks" in sp or "ask" in sp


def test_system_prompt_notes_canva_deferred(manifest: Manifest) -> None:
    sp = manifest.system_prompt.lower()
    assert "canva" in sp
    assert (
        "follow-up" in sp
        or "not wired" in sp
        or "isn't wired" in sp
        or "pending" in sp
    )


def test_system_prompt_roughly_under_token_budget(manifest: Manifest) -> None:
    """One token per ~4 chars — keep under ~1000 tokens. Over that,
    reshape into a shorter prompt + per-turn guidance from the
    planner."""
    assert len(manifest.system_prompt) < 4000


# ── Sandbox + memory ────────────────────────────────────────────


def test_manifest_sandbox_is_process(manifest: Manifest) -> None:
    assert manifest.sandbox.type == "process"


def test_manifest_memory_namespace(manifest: Manifest) -> None:
    assert manifest.memory_namespace == "pitch_deck_agent"


# ── Cross-agent hygiene ─────────────────────────────────────────


def test_manifest_tools_are_unique_from_web_design(manifest: Manifest) -> None:
    """web_design_agent and pitch_deck_agent share fs_read / fs_write
    / net_fetch but otherwise do different jobs. Catch accidental
    tool-list duplication that would blur the separation."""
    web_design_path = (
        Path(__file__).resolve().parents[1]
        / "agents"
        / "web_design_agent"
        / "manifest.yaml"
    )
    if not web_design_path.exists():
        pytest.skip("web_design_agent manifest not in this branch yet")
    web_design = Manifest.load(web_design_path)
    overlap = set(manifest.tools) & set(web_design.tools)
    # The shared utility tools are fine; reject sharing
    # *specialist* tools that'd confuse the planner.
    shared = {"fs_read", "fs_write", "net_fetch"}
    specialist_overlap = overlap - shared
    assert not specialist_overlap, (
        f"pitch_deck_agent + web_design_agent share specialist tools: "
        f"{specialist_overlap}"
    )
