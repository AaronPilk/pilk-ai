"""Rule-based task-complexity router.

Produces a Tier choice from a user goal without calling an LLM. Simple
keyword + length heuristics — fast, deterministic, zero cost. An
LLM-assisted classifier can replace this later without changing the
Governor/Orchestrator contract.

``classify_tier`` is the cheap default used by the Governor on every
plan. ``tier_classifier`` is a richer variant that also considers
``complexity_hint`` and ``expected_tool_calls`` when the caller has
them — used by agent playbooks that know up-front whether a run
really needs heavy reasoning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.governor.tiers import Tier

# Keywords / phrases that strongly suggest high-complexity reasoning or
# code/agent generation. A match upgrades to PREMIUM.
_PREMIUM_PATTERNS = [
    r"\bbuild (me )?(a |an )?agent\b",
    r"\bcreate (a |an )?agent\b",
    r"\bnew agent\b",
    r"\bwrite (some )?code\b",
    r"\brefactor\b",
    r"\bdebug\b",
    r"\bimplement\b",
    r"\barchitect\b",
    r"\bdesign the\b",
    r"\bdeep analysis\b",
    r"\bthorough(ly)?\b",
    r"\bdetailed plan\b",
    r"\bstep[- ]by[- ]step plan\b",
    r"\bstrategy\b",
    r"\bbusiness plan\b",
    r"\bmulti[- ]step\b",
    r"\bend[- ]to[- ]end\b",
]

# Short conversational markers that strongly suggest a chat turn. A
# match keeps us at LIGHT even if the string passes length heuristics.
_LIGHT_PATTERNS = [
    r"^(hi|hello|hey|yo|sup)\b",
    r"^(thanks|thank you|cheers)\b",
    r"^(how are you|how's it going|how's your day)",
    r"^(what time|what's up|what's the time)",
    r"^(ok|okay|sure|yes|no|yep|nope)\b",
    r"^(tell me (a )?joke|say hi|say hello)\b",
    r"^(good (morning|afternoon|evening|night))\b",
]

_PREMIUM_RE = [re.compile(p, re.IGNORECASE) for p in _PREMIUM_PATTERNS]
_LIGHT_RE = [re.compile(p, re.IGNORECASE) for p in _LIGHT_PATTERNS]


# The Telegram bridge composes a rolling window into the goal string as
# ``[Conversation so far — rolling window]\n… history …\n\n[New message]\n<msg>``.
# If we classify over the whole blob, every "refactor"/"implement"/
# "debug" said in recent scrollback keeps the tier pinned to PREMIUM —
# so plain conversational turns ("what do you think?") keep hitting
# Opus. Strip back to just the new message before classifying; other
# callers pass a bare goal string and see a no-op.
_NEW_MESSAGE_MARKER = "[New message]"


def _latest_user_turn(goal: str) -> str:
    """Return only the latest operator turn from a composed goal.

    If ``goal`` contains the Telegram bridge's ``[New message]`` marker,
    return everything after the first occurrence (trimmed). Otherwise
    return ``goal`` unchanged. Keeps the classifier honest about what
    the operator actually just asked for.
    """
    if not goal:
        return ""
    idx = goal.find(_NEW_MESSAGE_MARKER)
    if idx == -1:
        return goal
    tail = goal[idx + len(_NEW_MESSAGE_MARKER):]
    return tail.lstrip("\n\r ").rstrip()


def classify_tier(goal: str) -> Tier:
    """Classify an incoming user goal into a tier.

    Precedence: premium keywords > light markers > length heuristic.
    """
    g = _latest_user_turn(goal).strip() if goal else ""
    if not g:
        return Tier.LIGHT

    for rx in _PREMIUM_RE:
        if rx.search(g):
            return Tier.PREMIUM

    for rx in _LIGHT_RE:
        if rx.match(g):
            return Tier.LIGHT

    # Very short queries default to LIGHT; long ones to STANDARD. We stay
    # conservative — STANDARD handles most real work, PREMIUM only fires
    # on explicit high-complexity signals.
    if len(g) < 40:
        return Tier.LIGHT
    return Tier.STANDARD


@dataclass(frozen=True)
class TierDecision:
    """Rich classification output with scoring telemetry.

    Kept separate from the bare :func:`classify_tier` return value so
    the governor contract stays a plain ``Tier`` — the richer shape
    feeds the ledger / audit log when a caller wants to record WHY
    the decision happened, not just WHAT was picked.
    """

    tier: Tier
    reason: str
    score: int
    signals: dict[str, int]

    def to_public(self) -> dict:
        return {
            "tier": self.tier.value,
            "reason": self.reason,
            "score": self.score,
            "signals": dict(self.signals),
        }


# Thresholds turn the accumulated score into a tier. They're kept
# loose so the signal mix can evolve without churning the enum.
SCORE_LIGHT_MAX = 2
SCORE_STANDARD_MAX = 6


def tier_classifier(
    goal: str,
    *,
    expected_tool_calls: int = 0,
    complexity_hint: int = 0,
    has_attachments: bool = False,
) -> TierDecision:
    """Richer classifier — message length + tool calls + complexity.

    Returns a :class:`TierDecision` carrying the chosen tier, the
    dominant reason string, and the signal-by-signal score breakdown.
    Signals are additive:

    * ``premium_keyword`` — +7  (e.g. "refactor", "build an agent")
    * ``light_opener``    — -3  ("hi", "thanks")
    * ``len_long``        — +2  (goal > 240 chars)
    * ``len_medium``      — +1  (goal > 80 chars)
    * ``len_very_short``  — -2  (goal < 40 chars)
    * ``tool_calls``      — +min(expected, 5) (one point per expected call)
    * ``complexity_hint`` — +max(0, min(hint, 5))
    * ``attachments``     — +1  (forces at least STANDARD via orchestrator)

    Score → tier:
      score ≤ SCORE_LIGHT_MAX      → LIGHT
      score ≤ SCORE_STANDARD_MAX   → STANDARD
      score >  SCORE_STANDARD_MAX  → PREMIUM
    """
    signals: dict[str, int] = {}
    reason = "length_heuristic"
    # Same rolling-window extraction as classify_tier — the rich
    # classifier should score the latest turn, not the whole history.
    g = _latest_user_turn(goal or "").strip()

    # Hard-short-circuit on premium keywords so the explicit signal
    # always wins even when the message is short.
    for rx in _PREMIUM_RE:
        if rx.search(g):
            signals["premium_keyword"] = 7
            reason = "premium_keyword"
            break

    if reason != "premium_keyword":
        for rx in _LIGHT_RE:
            if rx.match(g):
                signals["light_opener"] = -3
                reason = "light_opener"
                break

    if len(g) > 240:
        signals["len_long"] = 2
    elif len(g) > 80:
        signals["len_medium"] = 1
    elif len(g) < 40:
        signals["len_very_short"] = -2

    if expected_tool_calls > 0:
        signals["tool_calls"] = min(int(expected_tool_calls), 5)
    if complexity_hint > 0:
        signals["complexity_hint"] = max(0, min(int(complexity_hint), 5))
    if has_attachments:
        signals["attachments"] = 1

    score = sum(signals.values())
    if score <= SCORE_LIGHT_MAX:
        tier = Tier.LIGHT
    elif score <= SCORE_STANDARD_MAX:
        tier = Tier.STANDARD
    else:
        tier = Tier.PREMIUM
    # When a deterministic keyword fired we keep that as the reason;
    # otherwise the final driver was the score math.
    if reason == "length_heuristic" and score > 0:
        reason = "score"
    return TierDecision(
        tier=tier,
        reason=reason,
        score=score,
        signals=signals,
    )


__all__ = [
    "SCORE_LIGHT_MAX",
    "SCORE_STANDARD_MAX",
    "TierDecision",
    "classify_tier",
    "tier_classifier",
]
