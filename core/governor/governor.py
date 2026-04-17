"""Governor — tier routing + budget gate.

Single entry point the orchestrator uses per turn:

    choice = governor.pick(goal, override=user_override)
    await governor.check_budget()  # may raise BudgetExceededError

`pick` applies, in order:

1. An explicit session override ("light" | "standard" | "premium") — wins outright.
2. The rule-based classifier (router.classify_tier).
3. The premium gate: when enabled and the classifier chose PREMIUM,
   downgrade to STANDARD and mark `gated=True` so the UI can surface
   an approval prompt (wired in Batch D).

Batch C executes only the Anthropic provider. If a tier's provider is
non-anthropic the orchestrator logs a fallback and uses the Anthropic
slot's model name as-is — we don't silently swap tiers. Real OpenAI
execution lands in Batch D.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.governor.budget import BudgetExceededError, DailyBudget
from core.governor.router import classify_tier
from core.governor.tiers import Tier, Tiers
from core.logging import get_logger

log = get_logger("pilkd.governor")

OverrideMode = Literal["auto", "light", "standard", "premium"]
PremiumGate = Literal["ask", "auto"]


@dataclass
class TierChoice:
    tier: Tier
    provider: str
    model: str
    reason: str  # "override" | "rule" | "gated_from_premium"
    gated: bool = False  # True when premium_gate downgraded us to standard

    def to_public(self) -> dict:
        return {
            "tier": self.tier.value,
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "gated": self.gated,
        }


class Governor:
    def __init__(
        self,
        tiers: Tiers,
        budget: DailyBudget,
        premium_gate: PremiumGate = "ask",
    ) -> None:
        self._tiers = tiers
        self._budget = budget
        self._premium_gate: PremiumGate = premium_gate
        self._override: OverrideMode = "auto"

    # ── config ───────────────────────────────────────────────────────

    @property
    def tiers(self) -> Tiers:
        return self._tiers

    @property
    def premium_gate(self) -> PremiumGate:
        return self._premium_gate

    def set_override(self, mode: OverrideMode) -> None:
        if mode not in ("auto", "light", "standard", "premium"):
            raise ValueError(f"invalid override: {mode}")
        self._override = mode
        log.info("governor_override_set", mode=mode)

    @property
    def override(self) -> OverrideMode:
        return self._override

    # ── decision ─────────────────────────────────────────────────────

    def pick(self, goal: str, override: OverrideMode | None = None) -> TierChoice:
        mode: OverrideMode = override or self._override
        if mode == "light":
            return self._materialise(Tier.LIGHT, reason="override")
        if mode == "standard":
            return self._materialise(Tier.STANDARD, reason="override")
        if mode == "premium":
            return self._materialise(Tier.PREMIUM, reason="override")

        chosen = classify_tier(goal)
        if chosen is Tier.PREMIUM and self._premium_gate == "ask":
            # Downgrade to STANDARD; the UI can later surface an approval
            # and call set_override('premium') to run this turn premium.
            log.info(
                "governor_premium_gated",
                goal_len=len(goal or ""),
                downgraded_to=Tier.STANDARD.value,
            )
            return self._materialise(
                Tier.STANDARD, reason="gated_from_premium", gated=True
            )
        return self._materialise(chosen, reason="rule")

    def _materialise(
        self, tier: Tier, *, reason: str, gated: bool = False
    ) -> TierChoice:
        spec = self._tiers.get(tier)
        provider = spec.provider
        if provider != "anthropic":
            log.warning(
                "governor_provider_fallback",
                tier=tier.value,
                requested_provider=provider,
                effective_provider="anthropic",
                detail=f"Batch C executes Anthropic only; provider={provider} will route here until Batch D",
            )
        return TierChoice(
            tier=tier,
            provider=spec.provider,
            model=spec.model,
            reason=reason,
            gated=gated,
        )

    # ── budget ───────────────────────────────────────────────────────

    async def check_budget(self) -> None:
        await self._budget.check()

    async def snapshot(self) -> dict:
        budget = await self._budget.snapshot()
        return {
            "tiers": self._tiers.to_public(),
            "override": self._override,
            "premium_gate": self._premium_gate,
            "budget": budget.to_public(),
        }


__all__ = ["BudgetExceededError", "Governor", "OverrideMode", "TierChoice"]
