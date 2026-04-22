"""Cross-session memory hydration.

Every new conversation turn starts with the orchestrator asking:
"what do I already know about this operator?" This module answers
that question by pulling structured memory entries + recent daily
notes from the brain vault, ordering them by priority, capping the
total token budget, and rendering a compact ``memory_context`` block
the orchestrator prepends to the system prompt.

### Why here, not in the orchestrator

Keeping the hydration logic in its own module means:

* The same pass fires on daemon boot (cold cache warm-up), on the
  first turn of every ``orchestrator.run`` / ``agent_run``, and on
  every Telegram message — one code path, one source of truth.
* Unit tests can exercise the ordering and token-budget rules
  without spinning an orchestrator.
* The Telegram bridge can reuse the block directly when it injects
  context into the rolling chat preamble.

### Ordering (never reshuffled)

1. ``standing_instructions`` — durable operator rules ("never email
   after 9pm"). Never truncated; the operator relies on these.
2. Recent daily notes — the last 7 days of ``daily/YYYY-MM-DD.md``
   entries from the brain vault, newest first.
3. ``facts`` — durable facts ("my assistant's name is Maria").
4. ``patterns`` — observed recurring behaviours.
5. ``preferences`` — soft likes / dislikes.
6. Topical brain notes — titles surfaced from the last 3 user
   messages. Optional; only fires when ``topic_hints`` is passed in.

### Token budget

A soft cap of 4000 tokens (rough chars/4 approximation — good enough
for a governor that already leaves headroom in every request). When
the render blows past the cap, we truncate in this order:

    preferences → patterns → facts → topical notes → daily notes

``standing_instructions`` are sacred. If they alone exceed the cap,
we keep them whole and emit a warning log line so the operator sees
they need pruning.

### Caching

Hydration hits SQLite + the filesystem on every call but both are
cheap: memory_entries is indexed on ``kind`` + ``created_at``, and
the daily notes scan is at most 7 small markdown files. A per-turn
in-memory cache would be premature — measured cost is < 5ms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable

from core.brain import Vault
from core.logging import get_logger
from core.memory.store import MemoryEntry, MemoryKind, MemoryStore

log = get_logger("pilkd.memory.hydration")

# Soft cap on the rendered memory_context block. Applied as ``chars //
# 4`` since the real tokenizer isn't available to this module — close
# enough in practice for Claude's BPE. The orchestrator sets
# max_tokens=16000 on every turn, so 4k of context leaves room for
# the conversation + tool loop output.
DEFAULT_TOKEN_CAP = 4000
# How many days of daily notes to pull. 7 covers a natural work week;
# anything older belongs in facts/patterns, not in the per-turn preamble.
DEFAULT_DAILY_WINDOW_DAYS = 7
# Topical-note lookup: we run brain.search on each hint term and keep
# up to this many hits per term so a chatty user doesn't drown out
# the rest of the block.
TOPIC_HITS_PER_TERM = 3
# Rough chars-per-token estimate. Good-enough for the budget math;
# the real token count is off by ±20% on natural prose which is well
# within our headroom.
CHARS_PER_TOKEN = 4
# Minimum word length for topic extraction — anything shorter is
# noise ("is", "the", "a"). We also strip a small English stopword
# set below so "the last thing you said" doesn't match every note.
_TOPIC_MIN_LEN = 4

_STOPWORDS = frozenset({
    "about", "after", "again", "also", "around", "because", "been",
    "being", "between", "both", "could", "does", "doing", "done",
    "during", "each", "every", "from", "have", "having", "here",
    "into", "just", "like", "more", "most", "much", "over", "said",
    "same", "should", "some", "such", "than", "that", "them", "then",
    "there", "these", "they", "this", "those", "through", "under",
    "very", "want", "wants", "what", "when", "where", "which",
    "while", "will", "with", "would", "your", "yours", "okay", "please",
    "think", "really", "going", "something", "anything", "everything",
    "someone", "anyone", "everyone", "because", "before",
})


@dataclass(frozen=True)
class HydratedContext:
    """The rendered block + a small accounting dict for observability.

    ``body`` is the actual markdown PILK prepends to the system
    prompt. ``stats`` surfaces per-section counts so the orchestrator
    can log a single structured line per turn instead of re-counting
    downstream.
    """

    body: str
    stats: dict[str, int]

    def is_empty(self) -> bool:
        return not self.body.strip()


def extract_topics(messages: Iterable[str], *, max_terms: int = 6) -> list[str]:
    """Pull topical keywords out of recent user messages.

    Cheap heuristic: lowercase, strip punctuation, drop stopwords +
    short tokens, keep the longest unique words. Not a substitute for
    a real entity extractor but enough to pick up "invoice", "skyway",
    "gold" out of a sentence without an LLM call.
    """
    seen: dict[str, None] = {}
    for msg in messages:
        if not msg:
            continue
        # Strip punctuation aggressively; keep intra-word hyphens
        # (so "xauusd" wins over "xau usd" and "sub-account" stays
        # whole).
        normalized = re.sub(r"[^a-z0-9\-]+", " ", msg.lower())
        for word in normalized.split():
            if len(word) < _TOPIC_MIN_LEN:
                continue
            if word in _STOPWORDS:
                continue
            if word.isdigit():
                continue
            seen.setdefault(word, None)
    # Order by first appearance; take the longest words first because
    # they carry the most signal ("invoicing" > "time").
    ordered = sorted(seen.keys(), key=lambda w: (-len(w), w))
    return ordered[:max_terms]


async def hydrate(
    *,
    store: MemoryStore,
    vault: Vault | None,
    topic_hints: Iterable[str] | None = None,
    token_cap: int = DEFAULT_TOKEN_CAP,
    daily_window_days: int = DEFAULT_DAILY_WINDOW_DAYS,
    now: datetime | None = None,
) -> HydratedContext:
    """Assemble the memory_context block for one turn.

    ``vault`` is optional — callers that don't have a brain vault
    attached (early boot, certain tests) get the structured-memory
    side only. ``topic_hints`` is usually the output of
    :func:`extract_topics` on the last three user messages, but any
    iterable of strings works.
    """
    clock = now or datetime.now(UTC)
    try:
        entries = await store.list()
    except Exception as e:  # pragma: no cover — defensive
        log.warning("memory_hydrate_list_failed", error=str(e))
        entries = []

    by_kind: dict[str, list[MemoryEntry]] = {
        MemoryKind.STANDING_INSTRUCTION.value: [],
        MemoryKind.FACT.value: [],
        MemoryKind.PATTERN.value: [],
        MemoryKind.PREFERENCE.value: [],
    }
    for e in entries:
        bucket = by_kind.setdefault(e.kind, [])
        bucket.append(e)

    daily_notes = _collect_daily_notes(
        vault, window=daily_window_days, now=clock,
    ) if vault is not None else []

    hints = [h for h in (topic_hints or []) if h]
    topical = _collect_topical_notes(vault, hints) if (
        vault is not None and hints
    ) else []

    stats = {
        "standing_instructions": len(by_kind[MemoryKind.STANDING_INSTRUCTION.value]),
        "facts": len(by_kind[MemoryKind.FACT.value]),
        "patterns": len(by_kind[MemoryKind.PATTERN.value]),
        "preferences": len(by_kind[MemoryKind.PREFERENCE.value]),
        "daily_notes": len(daily_notes),
        "topical_notes": len(topical),
    }

    sections = _render_sections(
        standing=by_kind[MemoryKind.STANDING_INSTRUCTION.value],
        facts=by_kind[MemoryKind.FACT.value],
        patterns=by_kind[MemoryKind.PATTERN.value],
        preferences=by_kind[MemoryKind.PREFERENCE.value],
        daily_notes=daily_notes,
        topical_notes=topical,
    )

    body = _apply_budget(sections, token_cap=token_cap)
    final_chars = len(body)
    stats["chars"] = final_chars
    stats["approx_tokens"] = final_chars // CHARS_PER_TOKEN
    log.info(
        "memory_hydrated",
        **stats,
    )
    return HydratedContext(body=body, stats=stats)


# ── rendering ─────────────────────────────────────────────────────


@dataclass
class _Section:
    """One labelled block in the rendered context.

    ``drop_priority`` governs which sections get truncated first under
    budget pressure — lower numbers go first. ``critical`` sections
    are never truncated even if they alone exceed the budget.
    """

    label: str
    header: str
    lines: list[str]
    drop_priority: int
    critical: bool = False

    def render(self) -> str:
        if not self.lines:
            return ""
        return "\n".join([self.header, *self.lines])

    def total_chars(self) -> int:
        return len(self.render())


def _render_sections(
    *,
    standing: list[MemoryEntry],
    facts: list[MemoryEntry],
    patterns: list[MemoryEntry],
    preferences: list[MemoryEntry],
    daily_notes: list[tuple[str, str]],
    topical_notes: list[tuple[str, str]],
) -> list[_Section]:
    """Turn each category into a labelled section in priority order."""
    sections: list[_Section] = []
    # Standing instructions — ALWAYS first, never truncated.
    if standing:
        sections.append(_Section(
            label="standing_instructions",
            header="## Standing instructions",
            lines=[_render_entry(e) for e in standing],
            drop_priority=99,
            critical=True,
        ))
    # Daily notes — newest first; truncated last before standing.
    if daily_notes:
        lines = []
        for date_label, body in daily_notes:
            snippet = body.strip()
            if not snippet:
                continue
            lines.append(f"### {date_label}")
            lines.append(snippet)
        if lines:
            sections.append(_Section(
                label="daily_notes",
                header="## Recent daily notes",
                lines=lines,
                drop_priority=4,
            ))
    if facts:
        sections.append(_Section(
            label="facts",
            header="## Facts",
            lines=[_render_entry(e) for e in facts],
            drop_priority=2,
        ))
    if patterns:
        sections.append(_Section(
            label="patterns",
            header="## Patterns",
            lines=[_render_entry(e) for e in patterns],
            drop_priority=1,
        ))
    if preferences:
        sections.append(_Section(
            label="preferences",
            header="## Preferences",
            lines=[_render_entry(e) for e in preferences],
            drop_priority=0,
        ))
    if topical_notes:
        lines = []
        for title, snippet in topical_notes:
            if not snippet.strip():
                continue
            lines.append(f"- [[{title}]] — {snippet.strip()}")
        if lines:
            sections.append(_Section(
                label="topical_notes",
                header="## Related brain notes",
                lines=lines,
                drop_priority=3,
            ))
    return sections


def _render_entry(entry: MemoryEntry) -> str:
    body = (entry.body or "").strip().replace("\n", " ")
    if body:
        return f"- **{entry.title}** — {body}"
    return f"- **{entry.title}**"


def _apply_budget(sections: list[_Section], *, token_cap: int) -> str:
    """Truncate in ``drop_priority`` order until under the cap.

    ``token_cap`` is converted to a char budget via CHARS_PER_TOKEN.
    We render one section at a time, check against the remaining
    budget, and drop non-critical sections when we overflow. Critical
    sections are always included even if they alone blow the cap.
    """
    if not sections:
        return ""
    char_budget = max(0, int(token_cap) * CHARS_PER_TOKEN)
    # Always-include set: critical sections go in first.
    included: list[_Section] = [s for s in sections if s.critical]
    used = sum(s.total_chars() + 2 for s in included)  # +2 for blank line sep
    if used > char_budget:
        log.warning(
            "memory_hydration_critical_exceeds_budget",
            chars=used,
            budget=char_budget,
        )
    # Now fold in the rest, highest drop_priority first (lowest = drop
    # first under pressure, so highest = include first).
    optional = sorted(
        (s for s in sections if not s.critical),
        key=lambda s: s.drop_priority,
        reverse=True,
    )
    for s in optional:
        cost = s.total_chars() + 2
        if used + cost <= char_budget:
            included.append(s)
            used += cost
    # Preserve the original section ordering in the final render so the
    # reader sees standing→daily→facts→patterns→preferences regardless
    # of drop order.
    included_sorted = sorted(
        included,
        key=lambda s: sections.index(s),
    )
    blocks = [s.render() for s in included_sorted if s.render()]
    if not blocks:
        return ""
    return "[memory_context]\n\n" + "\n\n".join(blocks) + "\n[/memory_context]"


# ── vault helpers ────────────────────────────────────────────────


def _collect_daily_notes(
    vault: Vault, *, window: int, now: datetime,
) -> list[tuple[str, str]]:
    """Return ``(YYYY-MM-DD, body)`` for each recent daily note.

    Reads at most ``window`` files. Missing files are silently skipped
    — an operator who hasn't journaled every day is normal, not an
    error. Bodies are trimmed to the first 20 lines so a runaway
    daily note doesn't swamp the block.
    """
    results: list[tuple[str, str]] = []
    for offset in range(int(window)):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        rel = f"daily/{day}.md"
        try:
            body = vault.read(rel)
        except FileNotFoundError:
            continue
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "memory_hydrate_daily_read_failed",
                path=rel,
                error=str(e),
            )
            continue
        lines = body.splitlines()
        trimmed = "\n".join(lines[:20]).strip()
        if trimmed:
            results.append((day, trimmed))
    return results


def _collect_topical_notes(
    vault: Vault, hints: list[str], *, per_term: int = TOPIC_HITS_PER_TERM,
) -> list[tuple[str, str]]:
    """For each hint term, surface up to ``per_term`` matching notes.

    Deduplicates on title — one note per match is plenty. Snippets come
    from the vault's built-in search, which is the same substring engine
    the brain_search tool exposes.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for term in hints:
        try:
            hits = vault.search(term, limit=per_term)
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "memory_hydrate_topic_search_failed",
                term=term,
                error=str(e),
            )
            continue
        for hit in hits:
            title = _title_from_path(hit.path)
            if title in seen:
                continue
            seen.add(title)
            out.append((title, hit.snippet))
    return out


def _title_from_path(path: str) -> str:
    """Turn ``clients/skyway/offer-a.md`` into ``offer-a``. Matches
    how Obsidian derives the display title when a path lacks a
    front-matter ``title`` key."""
    name = path.rsplit("/", 1)[-1]
    if name.lower().endswith(".md"):
        name = name[:-3]
    return name or path


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_DAILY_WINDOW_DAYS",
    "DEFAULT_TOKEN_CAP",
    "HydratedContext",
    "extract_topics",
    "hydrate",
]
