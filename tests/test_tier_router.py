"""Tier router — rolling-window extraction and keyword/length scoring."""
from __future__ import annotations

from core.governor.router import (
    TierDecision,
    _latest_user_turn,
    classify_tier,
    tier_classifier,
)
from core.governor.tiers import Tier

# ── _latest_user_turn ────────────────────────────────────────────────


def test_latest_user_turn_no_marker_is_noop() -> None:
    """Bare goals (dashboard / web / API callers) pass through."""
    assert _latest_user_turn("what do you think?") == "what do you think?"


def test_latest_user_turn_extracts_after_marker() -> None:
    """Composed Telegram prompt → only the trailing new message."""
    composed = (
        "[Conversation so far — rolling window]\n"
        "Me: please refactor the brain tab and implement wiki-linking\n"
        "PILK: OK, here is the plan…\n"
        "\n"
        "[New message]\n"
        "what do you think?"
    )
    assert _latest_user_turn(composed) == "what do you think?"


def test_latest_user_turn_handles_empty() -> None:
    assert _latest_user_turn("") == ""
    assert _latest_user_turn(None) == ""  # type: ignore[arg-type]


# ── classify_tier ────────────────────────────────────────────────────


def test_classify_tier_ignores_premium_words_in_history() -> None:
    """Repro of the Opus-too-often bug: a rolling window packed with
    premium keywords must NOT upgrade a plain conversational turn."""
    composed = (
        "[Conversation so far — rolling window]\n"
        "Me: refactor the orchestrator and implement a new agent\n"
        "Me: debug that tool call and design the deployment\n"
        "PILK: done\n"
        "\n"
        "[New message]\n"
        "what do you think?"
    )
    assert classify_tier(composed) is Tier.LIGHT


def test_classify_tier_still_upgrades_on_explicit_request() -> None:
    """Premium keywords in the NEW message still escalate — the fix
    only strips the history, not the genuine signal."""
    composed = (
        "[Conversation so far — rolling window]\n"
        "Me: hi\n"
        "PILK: hey\n"
        "\n"
        "[New message]\n"
        "please refactor the brain route to stream uploads"
    )
    assert classify_tier(composed) is Tier.PREMIUM


def test_classify_tier_bare_goal_unchanged() -> None:
    """Non-Telegram callers (dashboard chat, API) pass a bare string."""
    assert classify_tier("hi") is Tier.LIGHT
    assert classify_tier("refactor the billing module") is Tier.PREMIUM
    long_goal = "please help me plan out the launch " * 6  # > 40 chars
    assert classify_tier(long_goal) is Tier.STANDARD


# ── tier_classifier (rich) ───────────────────────────────────────────


def test_tier_classifier_scores_latest_turn_only() -> None:
    composed = (
        "[Conversation so far — rolling window]\n"
        "Me: refactor and implement multi-step strategy\n"
        "PILK: sure\n"
        "\n"
        "[New message]\n"
        "thanks!"
    )
    decision: TierDecision = tier_classifier(composed)
    assert decision.tier is Tier.LIGHT
    assert decision.reason == "light_opener"
    assert "premium_keyword" not in decision.signals
