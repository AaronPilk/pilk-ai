"""Tier definitions for the cost / routing governor.

Three tiers — light, standard, premium — each mapping to a concrete
(provider, model) pair. The provider field is recognised from day one
so OpenAI and other providers can slot in later without a schema change;
Batch C executes only the Anthropic path and falls back with a log line
if a non-anthropic provider is selected.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Tier(StrEnum):
    LIGHT = "light"
    STANDARD = "standard"
    PREMIUM = "premium"


@dataclass(frozen=True)
class TierSpec:
    tier: Tier
    provider: str  # "anthropic" | "openai" | ...
    model: str

    @property
    def label(self) -> str:
        """Human-facing label shown in the dashboard."""
        return {
            Tier.LIGHT: "Fast / Cheap",
            Tier.STANDARD: "Balanced",
            Tier.PREMIUM: "Deep Reasoning",
        }[self.tier]


@dataclass(frozen=True)
class Tiers:
    light: TierSpec
    standard: TierSpec
    premium: TierSpec

    def get(self, tier: Tier) -> TierSpec:
        return {
            Tier.LIGHT: self.light,
            Tier.STANDARD: self.standard,
            Tier.PREMIUM: self.premium,
        }[tier]

    def to_public(self) -> dict:
        return {
            "light": _spec_public(self.light),
            "standard": _spec_public(self.standard),
            "premium": _spec_public(self.premium),
        }


def _spec_public(s: TierSpec) -> dict:
    return {
        "tier": s.tier.value,
        "label": s.label,
        "provider": s.provider,
        "model": s.model,
    }
