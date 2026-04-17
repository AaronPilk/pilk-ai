"""Governor — tier routing + budget gate.

Single entry point the orchestrator uses per turn:

    choice = governor.pick(goal, override=user_override)
    await governor.check_budget()  # may raise BudgetExceededError

`pick` applies, in order:

1. An explicit session override ("light" | "standard" | "premium") — wins outright.
2. The rule-based classifier (router.classify_tier).
3. The premium gate: when enabled and the classifier chose PREMIUM,
   downgrade to STANDARD and mark `gated=True` so the UI can surface
   an approval prompt (flow ships in Batch E).

The provider slot on each tier (anthropic / openai / ...) is dispatched
to by the orchestrator's provider registry; if the chosen provider is
not configured the orchestrator falls back to Anthropic and logs it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiosqlite

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
        db_path: Path | str | None = None,
    ) -> None:
        self._tiers = tiers
        self._budget = budget
        self._premium_gate: PremiumGate = premium_gate
        self._override: OverrideMode = "auto"
        self._db_path: str | None = str(db_path) if db_path else None

    async def hydrate_from_db(self) -> None:
        """Load any persisted prefs that override the env-seeded defaults.

        Called once at startup, after the schema exists. Silently does
        nothing if no db path is configured.
        """
        if not self._db_path:
            return
        try:
            async with aiosqlite.connect(self._db_path) as db, db.execute(
                "SELECT key, value FROM governor_prefs"
            ) as cur:
                rows = await cur.fetchall()
        except Exception as e:  # pragma: no cover — schema missing
            log.warning("governor_hydrate_failed", detail=str(e))
            return
        for key, value in rows:
            if key == "daily_cap_usd":
                try:
                    self._budget = DailyBudget(self._budget._db_path, cap_usd=float(value))
                except ValueError:
                    continue
            elif key == "premium_gate" and value in ("ask", "auto"):
                self._premium_gate = value  # type: ignore[assignment]
        log.info(
            "governor_hydrated",
            daily_cap_usd=self._budget.cap_usd,
            premium_gate=self._premium_gate,
        )

    async def _persist(self, key: str, value: str) -> None:
        if not self._db_path:
            return
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    "INSERT INTO governor_prefs(key, value, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, value, now),
                )
                await db.commit()
        except Exception as e:  # pragma: no cover
            log.warning("governor_persist_failed", key=key, detail=str(e))

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

    async def set_daily_cap(self, usd: float) -> None:
        if usd < 0:
            raise ValueError("daily cap must be >= 0")
        # Rewire the DailyBudget's cap in-place by rebuilding it — simpler
        # than exposing a setter on the budget class.
        self._budget = DailyBudget(
            db_path=self._budget._db_path,
            cap_usd=float(usd),
        )
        await self._persist("daily_cap_usd", str(float(usd)))
        log.info("governor_daily_cap_set", usd=usd)

    async def set_premium_gate(self, mode: PremiumGate) -> None:
        if mode not in ("ask", "auto"):
            raise ValueError(f"invalid premium_gate: {mode}")
        self._premium_gate = mode
        await self._persist("premium_gate", mode)
        log.info("governor_premium_gate_set", mode=mode)

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
