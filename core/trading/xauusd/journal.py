"""Structured journal for every agent decision.

Thin helper on top of the existing structlog logger so every state
transition, rule-engine verdict, risk decision, and order attempt
lands in one searchable stream with a consistent schema. The tool
layer calls these helpers instead of formatting log lines ad-hoc so
review + post-mortems aren't a treasure hunt.

Usage:

    from core.trading.xauusd.journal import (
        journal_state, journal_verdict, journal_order_attempt,
    )

    journal_state(before, after, reason, plan_id=...)

No separate file or table yet — writes flow through ``structlog`` into
whatever logging sinks the daemon has wired up. The Ledger hook lands
in PR C when real orders are placed.
"""

from __future__ import annotations

from typing import Any

from core.logging import get_logger
from core.trading.xauusd.state import StateTransition

log = get_logger("pilkd.xauusd")


def journal_state(
    transition: StateTransition,
    *,
    plan_id: str | None = None,
    agent_name: str = "xauusd_execution_agent",
) -> None:
    log.info(
        "xauusd.state",
        kind="state_transition",
        from_state=transition.from_state.value,
        to_state=transition.to_state.value,
        reason=transition.reason,
        at=transition.at,
        plan_id=plan_id,
        agent_name=agent_name,
    )


def journal_verdict(
    verdict: str,
    reason: str,
    *,
    details: dict[str, Any] | None = None,
    plan_id: str | None = None,
) -> None:
    log.info(
        "xauusd.verdict",
        kind="rule_engine_verdict",
        verdict=verdict,
        reason=reason,
        details=details or {},
        plan_id=plan_id,
    )


def journal_risk(
    *,
    accepted: bool,
    reason: str,
    lots: float | None = None,
    risk_usd: float | None = None,
    stop_distance_usd: float | None = None,
    plan_id: str | None = None,
) -> None:
    log.info(
        "xauusd.risk",
        kind="risk_decision",
        accepted=accepted,
        reason=reason,
        lots=lots,
        risk_usd=risk_usd,
        stop_distance_usd=stop_distance_usd,
        plan_id=plan_id,
    )


def journal_order_attempt(
    *,
    side: str,
    lots: float,
    entry: float,
    stop: float,
    take_profit: float | None,
    mode: str,
    placed: bool,
    broker_message: str,
    plan_id: str | None = None,
) -> None:
    """Log every order attempt (paper or live). ``placed`` reflects
    whether the broker said yes — ``mode`` makes "paper" attempts
    unambiguous in post-mortems."""
    log.info(
        "xauusd.order",
        kind="order_attempt",
        side=side,
        lots=lots,
        entry=entry,
        stop=stop,
        take_profit=take_profit,
        mode=mode,
        placed=placed,
        broker_message=broker_message,
        plan_id=plan_id,
    )


def journal_safety_interrupt(
    *,
    reason: str,
    payload: dict[str, Any] | None = None,
    plan_id: str | None = None,
) -> None:
    """Always log at WARNING — these fire when a guardrail trips."""
    log.warning(
        "xauusd.safety",
        kind="safety_interrupt",
        reason=reason,
        payload=payload or {},
        plan_id=plan_id,
    )
