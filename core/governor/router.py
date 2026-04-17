"""Rule-based task-complexity router.

Produces a Tier choice from a user goal without calling an LLM. Simple
keyword + length heuristics — fast, deterministic, zero cost. An
LLM-assisted classifier can replace this later without changing the
Governor/Orchestrator contract.
"""

from __future__ import annotations

import re

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


def classify_tier(goal: str) -> Tier:
    """Classify an incoming user goal into a tier.

    Precedence: premium keywords > light markers > length heuristic.
    """
    if not goal:
        return Tier.LIGHT
    g = goal.strip()

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
