"""Cheap pre-chat classifier: goal -> agent, or None for Pilk.

When the user's message is a clear, unambiguous fit for a single
registered agent, we skip loading Pilk entirely and dispatch the goal
straight to ``orchestrator.agent_run`` — that's the token-efficiency
win that lets Pilk stay fast on obvious asks while the full planner
handles anything ambiguous or multi-step.

The classifier is deliberately conservative: when in doubt, fall
through to Pilk. Mis-routing is worse than routing through Pilk.

V1 implementation is keyword-overlap based (no LLM call). It can be
swapped for a Haiku call later without touching callers — the contract
is ``classify_agent(goal, manifests) -> (agent_name, confidence) | None``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from core.registry.manifest import Manifest

# Words that appear everywhere and carry no signal for routing.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can",
        "could", "did", "do", "does", "for", "from", "get", "got",
        "had", "has", "have", "he", "her", "hey", "him", "his", "how",
        "i", "if", "in", "is", "it", "its", "just", "let", "like",
        "may", "me", "my", "no", "not", "of", "on", "or", "our",
        "out", "please", "she", "should", "so", "some", "that", "the",
        "their", "them", "then", "there", "they", "this", "those",
        "to", "too", "up", "us", "was", "we", "were", "what", "when",
        "where", "which", "who", "why", "will", "with", "would", "you",
        "your", "yours", "now", "also", "any", "over", "into",
        "make", "made", "makes", "need", "needs", "want", "wants",
        "pilk", "hey", "okay", "ok", "yeah", "yes", "no", "thanks",
    }
)

# Agents that are infrastructure, not delegation targets. Users ask
# things like "what's Sentinel seeing" — that's a Pilk question, not
# an agent to hand off to.
_NON_DELEGABLE: frozenset[str] = frozenset({"sentinel"})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase + alphanumeric tokens, stopwords removed."""
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _agent_keywords(manifest: Manifest) -> set[str]:
    """Distinctive tokens for this agent, drawn from name + description.

    Name parts (split on ``_``) are weighted by being included directly;
    description tokens get the same treatment. Stopwords are dropped.
    ``_agent``/``_ops``/etc. suffix tokens are kept since they tend to
    be the strong signal (e.g. 'ads' from meta_ads_agent).
    """
    name_tokens = manifest.name.lower().split("_")
    desc_tokens = _tokenize(manifest.description or "")
    return {t for t in (*name_tokens, *desc_tokens) if t and t not in _STOPWORDS}


def classify_agent(
    goal: str,
    manifests: Iterable[Manifest],
    *,
    min_score: float = 0.34,
    min_gap: float = 0.18,
) -> tuple[str, float] | None:
    """Return (agent_name, score) if goal unambiguously fits one agent.

    ``min_score`` — absolute floor for the winning score. Too low and
    we mis-route short messages ("hi"); too high and we never route.
    0.34 ≈ roughly a third of a goal's content words landing in one
    agent's keyword bag.

    ``min_gap`` — how far the winner must lead the runner-up. Keeps us
    conservative when two agents are close (e.g. ``prospector_agent``
    vs ``sales_ops_agent`` for a leads ask).

    Returns None if no agent clears both bars, or if the registry is
    empty. None means "hand to Pilk" — that's the safe default.
    """
    goal_tokens = _tokenize(goal)
    if not goal_tokens:
        return None

    goal_set = set(goal_tokens)
    scores: list[tuple[str, float]] = []
    for m in manifests:
        if m.name in _NON_DELEGABLE:
            continue
        keywords = _agent_keywords(m)
        if not keywords:
            continue
        overlap = goal_set & keywords
        if not overlap:
            continue
        # Normalize by goal length — long goals aren't unfairly advantaged.
        score = len(overlap) / max(len(goal_set), 1)
        scores.append((m.name, score))

    if not scores:
        return None

    scores.sort(key=lambda p: p[1], reverse=True)
    best_name, best_score = scores[0]
    second_score = scores[1][1] if len(scores) > 1 else 0.0

    if best_score < min_score:
        return None
    if (best_score - second_score) < min_gap:
        return None
    return best_name, best_score


__all__ = ["classify_agent"]
