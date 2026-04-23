"""Vision-bypass: image turns skip the subscription CLI.

``claude_code`` routes through the ``claude`` binary as a subprocess
and has no vision surface. STANDARD tier now defaults to that
provider, so images would silently drop without this bypass. The
helper mutates a ``TierChoice`` in place to redirect the turn onto
the Anthropic API while preserving tier + model so cost accounting
stays coherent.
"""

from __future__ import annotations

from pathlib import Path

from core.governor import Tier
from core.governor.governor import TierChoice
from core.orchestrator.orchestrator import (
    ChatAttachment,
    _apply_vision_bypass,
)


def _choice(provider: str, reason: str = "rule") -> TierChoice:
    return TierChoice(
        tier=Tier.STANDARD, provider=provider,
        model="claude-sonnet-4-6", reason=reason,
    )


def _attachment(kind: str) -> ChatAttachment:
    return ChatAttachment(
        id="a1", kind=kind, mime="image/png",
        filename="x.png", path=Path("/tmp/x.png"),
    )


def test_noop_when_provider_is_not_claude_code() -> None:
    choice = _choice("anthropic")
    changed = _apply_vision_bypass(choice, [_attachment("image")])
    assert changed is False
    assert choice.provider == "anthropic"
    assert choice.reason == "rule"


def test_noop_when_no_image_present() -> None:
    choice = _choice("claude_code")
    changed = _apply_vision_bypass(choice, [_attachment("document")])
    assert changed is False
    assert choice.provider == "claude_code"


def test_noop_when_attachments_empty() -> None:
    choice = _choice("claude_code")
    changed = _apply_vision_bypass(choice, [])
    assert changed is False
    assert choice.provider == "claude_code"


def test_bypass_fires_when_claude_code_sees_image() -> None:
    choice = _choice("claude_code")
    changed = _apply_vision_bypass(choice, [_attachment("image")])
    assert changed is True
    assert choice.provider == "anthropic"
    assert choice.reason == "vision_bypass"
    # Tier + model preserved so cost accounting + tier metadata stay
    # coherent across the bypass.
    assert choice.tier == Tier.STANDARD
    assert choice.model == "claude-sonnet-4-6"


def test_bypass_preserves_non_default_reason() -> None:
    """An explicit 'gate_approved' or similar stays set so the
    audit trail reflects the original decision path, not the
    implementation detail of the bypass."""
    choice = _choice("claude_code", reason="gate_approved")
    changed = _apply_vision_bypass(choice, [_attachment("image")])
    assert changed is True
    assert choice.provider == "anthropic"
    assert choice.reason == "gate_approved"
