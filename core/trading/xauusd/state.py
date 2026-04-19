"""State machine for the XAUUSD execution agent.

The state model is deliberately small and explicit: every transition
has a written reason attached that the journaling layer captures in
the Ledger. The agent is never in a vague 'trading' state — it is
always in one of the enum values below.

Legal transitions are pinned in ``ALLOWED_TRANSITIONS``. Calling
``transition(new_state, reason)`` with an illegal pair raises; there
is no "best-effort" transition and no state-skip. If a transition you
expected isn't allowed, fix the rule engine — don't relax the table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class AgentState(StrEnum):
    OFF = "OFF"
    SCANNING = "SCANNING"
    WATCHLIST = "WATCHLIST"
    BIASED_LONG = "BIASED_LONG"
    BIASED_SHORT = "BIASED_SHORT"
    READY_LONG = "READY_LONG"
    READY_SHORT = "READY_SHORT"
    IN_POSITION = "IN_POSITION"
    COOLDOWN = "COOLDOWN"
    DISABLED = "DISABLED"


# Allowed transitions. The key is the "from" state; each value is the
# set of states we'll accept moving to from there. Every state can
# move to DISABLED — safety always wins.
_ALLOWED: dict[AgentState, frozenset[AgentState]] = {
    AgentState.OFF: frozenset({AgentState.SCANNING, AgentState.DISABLED}),
    AgentState.SCANNING: frozenset(
        {
            AgentState.WATCHLIST,
            AgentState.BIASED_LONG,
            AgentState.BIASED_SHORT,
            AgentState.COOLDOWN,
            AgentState.OFF,
            AgentState.DISABLED,
        }
    ),
    AgentState.WATCHLIST: frozenset(
        {
            AgentState.SCANNING,
            AgentState.BIASED_LONG,
            AgentState.BIASED_SHORT,
            AgentState.COOLDOWN,
            AgentState.DISABLED,
        }
    ),
    AgentState.BIASED_LONG: frozenset(
        {
            AgentState.READY_LONG,
            AgentState.SCANNING,     # bias invalidated
            AgentState.COOLDOWN,
            AgentState.DISABLED,
        }
    ),
    AgentState.BIASED_SHORT: frozenset(
        {
            AgentState.READY_SHORT,
            AgentState.SCANNING,
            AgentState.COOLDOWN,
            AgentState.DISABLED,
        }
    ),
    AgentState.READY_LONG: frozenset(
        {
            AgentState.IN_POSITION,
            AgentState.BIASED_LONG,  # setup faded without firing
            AgentState.SCANNING,
            AgentState.COOLDOWN,
            AgentState.DISABLED,
        }
    ),
    AgentState.READY_SHORT: frozenset(
        {
            AgentState.IN_POSITION,
            AgentState.BIASED_SHORT,
            AgentState.SCANNING,
            AgentState.COOLDOWN,
            AgentState.DISABLED,
        }
    ),
    AgentState.IN_POSITION: frozenset(
        {
            AgentState.COOLDOWN,     # trade closed, cool off before next
            AgentState.DISABLED,
        }
    ),
    AgentState.COOLDOWN: frozenset(
        {
            AgentState.SCANNING,
            AgentState.OFF,
            AgentState.DISABLED,
        }
    ),
    # DISABLED is deliberately sticky. Re-enabling is a human action
    # routed through the dashboard, which ends up at OFF / SCANNING.
    AgentState.DISABLED: frozenset({AgentState.OFF, AgentState.SCANNING}),
}


@dataclass(frozen=True)
class StateTransition:
    from_state: AgentState
    to_state: AgentState
    reason: str
    at: str   # ISO 8601 UTC


class IllegalTransitionError(ValueError):
    """Raised when a caller tries to move between states not listed in
    ``_ALLOWED``. Don't catch this to swallow it — fix the caller."""


@dataclass
class StateMachine:
    """In-memory state holder + transition log.

    The tool layer uses this inside a single plan turn; persistence
    across restarts lives in the Ledger via ``journal.record_transition``.
    """

    current: AgentState = AgentState.OFF
    history: list[StateTransition] = field(default_factory=list)

    def transition(self, to: AgentState, reason: str) -> StateTransition:
        if not reason or not reason.strip():
            raise ValueError("state transitions require a non-empty reason")
        allowed = _ALLOWED.get(self.current, frozenset())
        if to not in allowed:
            raise IllegalTransitionError(
                f"illegal transition {self.current.value} → {to.value}"
            )
        t = StateTransition(
            from_state=self.current,
            to_state=to,
            reason=reason.strip(),
            at=datetime.now(UTC).isoformat(),
        )
        self.history.append(t)
        self.current = to
        return t

    def force_disable(self, reason: str) -> StateTransition:
        """Shortcut that's always allowed — used by safety interrupts.

        Safety must always be able to disable the agent regardless of
        current state. This is the only state change that bypasses the
        transition table.
        """
        t = StateTransition(
            from_state=self.current,
            to_state=AgentState.DISABLED,
            reason=reason,
            at=datetime.now(UTC).isoformat(),
        )
        self.history.append(t)
        self.current = AgentState.DISABLED
        return t
