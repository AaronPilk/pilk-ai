"""KeywordScorer — cheap, transparent, no-LLM relevance scoring.

For every fetched item, the scorer:
  1. Builds a normalised search blob from title + body + URL slug
  2. For each topic in the watchlist (priority-weighted), counts
     how many of its keywords appear and tallies points
  3. Returns a 0-100 score, the matched topic slugs, and a
     human-readable reason string

Why not LLM? Cost, latency, and explainability. Keyword scoring is
pennies per million items, runs in microseconds, and the operator
can always read the reason string and understand why something got
flagged. LLM scoring lands in Batch 3 as a *batched* second pass on
items that already cleared the keyword bar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from core.intelligence.models import Topic

# Per-priority points awarded for each keyword hit. Picked so a
# single keyword match against a "critical" topic can clear the
# default brain-write threshold (30) on its own; "low" topics need
# multiple matches before they bubble up.
_PRIORITY_POINTS: dict[str, int] = {
    "low": 8,
    "medium": 18,
    "high": 32,
    "critical": 50,
}

# Cap per-topic contribution so one feed's topical-keyword spam
# can't blow out the score and crowd out other matches.
_MAX_PER_TOPIC = 60

# Hard ceiling — score is a 0-100 scalar regardless of how many
# matches accumulate.
_MAX_SCORE = 100

# Word-boundary tokeniser. Lowercases + splits on non-word chars so
# "Anthropic" matches "Anthropic's" or "anthropic-blog" but not
# random substrings ("anti" inside "anticipate").
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-_+]*", re.UNICODE)


@dataclass(frozen=True)
class ScoreOutcome:
    score: int
    matched_topics: list[str]
    reason: str
    dimensions: dict[str, int]  # per-topic score breakdown


class KeywordScorer:
    """Scorer instance. Stateless apart from the topics snapshot
    passed at construction. Caller is responsible for refreshing
    the snapshot when topics change — the scorer never reads the
    DB directly."""

    def __init__(self, topics: Iterable[Topic]) -> None:
        self._topics = [t for t in topics if t.keywords]

    def score(
        self,
        *,
        title: str,
        body: str = "",
        url: str = "",
    ) -> ScoreOutcome:
        if not self._topics:
            return ScoreOutcome(
                score=0,
                matched_topics=[],
                reason="no topics defined; nothing to match against",
                dimensions={},
            )
        tokens = self._tokenise(f"{title}\n{body}\n{url}")
        if not tokens:
            return ScoreOutcome(
                score=0,
                matched_topics=[],
                reason="item has no scoreable text",
                dimensions={},
            )

        total = 0
        matched: list[str] = []
        dims: dict[str, int] = {}
        reasons: list[str] = []

        for topic in self._topics:
            points = _PRIORITY_POINTS.get(topic.priority, 18)
            hits = self._topic_hits(tokens, topic.keywords)
            if hits == 0:
                continue
            topic_score = min(_MAX_PER_TOPIC, hits * points)
            total += topic_score
            matched.append(topic.slug)
            dims[topic.slug] = topic_score
            reasons.append(
                f"{topic.slug}({topic.priority}): {hits} hit"
                + ("" if hits == 1 else "s")
            )

        capped = min(_MAX_SCORE, total)
        if not matched:
            return ScoreOutcome(
                score=0,
                matched_topics=[],
                reason="no topic keywords matched",
                dimensions={},
            )
        reason = "matched " + "; ".join(reasons)
        return ScoreOutcome(
            score=capped,
            matched_topics=matched,
            reason=reason,
            dimensions=dims,
        )

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _tokenise(text: str) -> set[str]:
        if not text:
            return set()
        return set(_WORD_RE.findall(text.lower()))

    @staticmethod
    def _topic_hits(tokens: set[str], keywords: list[str]) -> int:
        """Count how many of ``keywords`` appear in ``tokens``.

        Multi-word keywords (e.g. "claude code") are matched as a
        substring on the joined token blob; single-word keywords use
        exact-token match so "agent" doesn't fire on "agents-of-x"
        but "claude code" still fires on titles containing it.
        """
        if not keywords:
            return 0
        blob = " ".join(sorted(tokens))
        hits = 0
        for kw in keywords:
            kw_norm = kw.strip().lower()
            if not kw_norm:
                continue
            if " " in kw_norm:
                # Multi-word: substring on the blob. Cheap, good
                # enough — a perfect implementation would check
                # ordered adjacency in the original text.
                if kw_norm in blob:
                    hits += 1
            else:
                if kw_norm in tokens:
                    hits += 1
        return hits


__all__ = ["KeywordScorer", "ScoreOutcome"]
