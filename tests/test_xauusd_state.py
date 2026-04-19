"""State-machine transition tests.

The transition table is pinned; these tests make sure that pin stays
intact. Every allowed transition has a positive test; the negative
tests cover a small but representative set of illegal moves.
"""

from __future__ import annotations

import pytest

from core.trading.xauusd.state import (
    AgentState,
    IllegalTransitionError,
    StateMachine,
)


def test_off_to_scanning_then_back() -> None:
    sm = StateMachine()
    assert sm.current is AgentState.OFF
    sm.transition(AgentState.SCANNING, "operator start")
    assert sm.current is AgentState.SCANNING
    sm.transition(AgentState.COOLDOWN, "no setups this session")
    assert sm.current is AgentState.COOLDOWN
    sm.transition(AgentState.OFF, "end of day")
    assert sm.current is AgentState.OFF


def test_happy_path_long() -> None:
    sm = StateMachine()
    sm.transition(AgentState.SCANNING, "start")
    sm.transition(AgentState.WATCHLIST, "price near resistance")
    sm.transition(AgentState.BIASED_LONG, "HH/HL confirmed")
    sm.transition(AgentState.READY_LONG, "5M engulfing")
    sm.transition(AgentState.IN_POSITION, "broker filled")
    sm.transition(AgentState.COOLDOWN, "target hit")
    sm.transition(AgentState.SCANNING, "cooldown elapsed")


def test_happy_path_short_mirrors_long() -> None:
    sm = StateMachine()
    sm.transition(AgentState.SCANNING, "start")
    sm.transition(AgentState.BIASED_SHORT, "LL/LH confirmed")
    sm.transition(AgentState.READY_SHORT, "5M bearish engulfing")
    sm.transition(AgentState.IN_POSITION, "broker filled")
    sm.transition(AgentState.COOLDOWN, "stop hit")


def test_illegal_transition_raises() -> None:
    sm = StateMachine()
    # Can't go from OFF directly to READY_LONG.
    with pytest.raises(IllegalTransitionError):
        sm.transition(AgentState.READY_LONG, "forcing")


def test_in_position_cannot_skip_cooldown() -> None:
    sm = StateMachine()
    sm.transition(AgentState.SCANNING, "start")
    sm.transition(AgentState.BIASED_LONG, "bull")
    sm.transition(AgentState.READY_LONG, "trigger")
    sm.transition(AgentState.IN_POSITION, "fill")
    # Must go through COOLDOWN (or DISABLED) — not straight back to
    # SCANNING or READY_*.
    with pytest.raises(IllegalTransitionError):
        sm.transition(AgentState.SCANNING, "skip")
    with pytest.raises(IllegalTransitionError):
        sm.transition(AgentState.READY_LONG, "re-enter immediately")


def test_reason_required() -> None:
    sm = StateMachine()
    with pytest.raises(ValueError, match="non-empty reason"):
        sm.transition(AgentState.SCANNING, "")
    with pytest.raises(ValueError, match="non-empty reason"):
        sm.transition(AgentState.SCANNING, "   ")


def test_force_disable_always_allowed() -> None:
    sm = StateMachine()
    # From every state, force_disable should succeed.
    for state in [
        AgentState.SCANNING,
        AgentState.WATCHLIST,
        AgentState.BIASED_LONG,
        AgentState.BIASED_SHORT,
        AgentState.READY_LONG,
        AgentState.READY_SHORT,
        AgentState.IN_POSITION,
        AgentState.COOLDOWN,
    ]:
        sm = StateMachine(current=state)
        t = sm.force_disable("circuit breaker")
        assert sm.current is AgentState.DISABLED
        assert t.reason == "circuit breaker"


def test_disabled_is_sticky() -> None:
    sm = StateMachine(current=AgentState.DISABLED)
    # Only OFF/SCANNING re-entries allowed from DISABLED.
    with pytest.raises(IllegalTransitionError):
        sm.transition(AgentState.READY_LONG, "bypass")
    with pytest.raises(IllegalTransitionError):
        sm.transition(AgentState.IN_POSITION, "bypass")
    # These are allowed.
    sm.transition(AgentState.SCANNING, "operator re-enabled")
    assert sm.current is AgentState.SCANNING


def test_history_records_every_transition() -> None:
    sm = StateMachine()
    sm.transition(AgentState.SCANNING, "start")
    sm.transition(AgentState.BIASED_LONG, "bull")
    sm.force_disable("test")
    assert len(sm.history) == 3
    assert sm.history[0].to_state is AgentState.SCANNING
    assert sm.history[-1].to_state is AgentState.DISABLED
    assert sm.history[-1].reason == "test"
