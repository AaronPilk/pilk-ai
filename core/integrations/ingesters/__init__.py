"""Brain-vault ingesters — read local / exported content, normalise
to markdown, and stage it for the Vault to land.

Each ingester exposes a pure function that:

1. Reads from a specific, well-known location (no arbitrary paths).
2. Produces a ``list[IngestedNote]`` — each carries a target vault-
   relative path + markdown body + a source identifier for dedupe.
3. Is idempotent — re-running over the same source writes the same
   files; later content replaces earlier.

The tool layer (`core/tools/builtin/brain_ingest.py`) handles the
vault writes; the ingesters stay pure so they're trivially testable
without a real vault.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IngestedNote:
    """One markdown note ready to land in the brain vault.

    ``path`` is vault-relative POSIX (e.g. ``ingested/claude-code/
    pilk-ai.md``). ``source_id`` is a stable identifier for the
    source (usually a filesystem path or a conversation ID) — the
    tool layer uses it to log what landed where.
    """

    path: str
    body: str
    source_id: str
    title: str


__all__ = ["IngestedNote"]
