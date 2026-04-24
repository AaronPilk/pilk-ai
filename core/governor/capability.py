"""Capability-based provider override.

The Governor's tier classifier picks between LIGHT / STANDARD /
PREMIUM based on task complexity. Each tier points at one
(provider, model) pair — today that's Anthropic across the board in
most configurations. This module adds an orthogonal axis: when the
task signals a specific capability that some providers handle
materially better or cheaper, we swap the provider for that turn.

Implemented capabilities:

* ``vision`` — goal has image attachments or explicitly mentions
  screenshots / visual inspection. Gemini handles vision at a
  fraction of Claude / GPT-4o's cost.
* ``long_context`` — goal text or document attachments push past
  a size threshold where Gemini's 1M-token window is meaningfully
  cheaper than Claude's 200k.

Anything else returns None — the tier's default provider wins.

The orchestrator consults :func:`classify_capability` after the
tier is picked; if the preferred provider is configured (present
in ``providers`` dict) it swaps; otherwise the tier choice stands
and we log the near-miss so the operator can wire the provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.orchestrator.orchestrator import ChatAttachment


class Capability(StrEnum):
    VISION = "vision"
    LONG_CONTEXT = "long_context"


# Goal-text length above which we start treating a request as
# long-context. 15k chars ≈ 3-4k tokens of pasted content, which is
# already a range where Gemini's context pricing pulls ahead.
_LONG_CONTEXT_CHAR_THRESHOLD = 15_000

# Phrases that strongly suggest the user wants visual reasoning even
# when no image attachment is present (they may reference an image
# elsewhere in the conversation).
_VISION_RE = re.compile(
    r"\b(screenshot|image|picture|photo|chart|diagram|look at (the|this) (image|pic|screenshot))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CapabilityHint:
    """Which capability applies and which provider we'd prefer.

    ``preferred_provider`` is the provider key we want (``"gemini"``,
    ``"openai"``, ...). The orchestrator verifies availability before
    honouring the hint.
    """

    capability: Capability
    preferred_provider: str
    reason: str


# Cross-provider model names keyed on (capability, provider). Keep
# this small and obvious; adding rows is how we extend routing. Each
# row is a best-cost-at-capability pick, not the provider's flagship.
CAPABILITY_PROVIDER_MODELS: dict[Capability, dict[str, str]] = {
    Capability.VISION: {
        # Gemini 2.0 Flash handles images well at meaningful cost
        # savings vs. Claude Sonnet with vision or GPT-4o.
        "gemini": "gemini-2.0-flash-exp",
        # GPT-4o is the OpenAI vision workhorse — slotted as a
        # secondary pick if Gemini isn't wired.
        "openai": "gpt-4o",
    },
    Capability.LONG_CONTEXT: {
        # Gemini 1.5 / 2.0 Pro have the 1M-token window — the clear
        # win for genuinely long inputs.
        "gemini": "gemini-2.0-pro-exp",
    },
}


def classify_capability(
    goal: str, attachments: list[ChatAttachment] | None = None,
) -> CapabilityHint | None:
    """Inspect the goal + attachments for a capability signal.

    Returns ``None`` when no capability hint applies — the caller
    keeps whatever tier provider was picked. The returned hint lists
    one provider (the preferred pick) even when multiple would fit;
    the orchestrator's fallback chain handles the "preferred not
    configured" case.
    """
    atts = attachments or []

    if any(a.kind == "image" for a in atts):
        return _hint_for(Capability.VISION, "image_attachment")
    if goal and _VISION_RE.search(goal):
        return _hint_for(Capability.VISION, "vision_keyword")

    # Rough long-context signal: goal text size, OR a document
    # attachment (which usually adds thousands of tokens once the
    # provider extracts it).
    if goal and len(goal) > _LONG_CONTEXT_CHAR_THRESHOLD:
        return _hint_for(Capability.LONG_CONTEXT, "goal_length")
    if any(a.kind == "document" for a in atts):
        return _hint_for(Capability.LONG_CONTEXT, "document_attachment")

    return None


def _hint_for(capability: Capability, reason: str) -> CapabilityHint | None:
    mapping = CAPABILITY_PROVIDER_MODELS.get(capability)
    if not mapping:
        return None
    preferred = next(iter(mapping.keys()))
    return CapabilityHint(
        capability=capability,
        preferred_provider=preferred,
        reason=reason,
    )


def resolve_model(capability: Capability, provider: str) -> str | None:
    """Return the best cross-provider model name for this capability
    on the given provider, or ``None`` if we don't have a mapping."""
    return CAPABILITY_PROVIDER_MODELS.get(capability, {}).get(provider)


__all__ = [
    "CAPABILITY_PROVIDER_MODELS",
    "Capability",
    "CapabilityHint",
    "classify_capability",
    "resolve_model",
]
