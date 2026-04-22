"""Memory layer — structured recall of what PILK is retaining.

This is the *structured* side of memory: explicit preferences, standing
instructions, remembered facts, and observed patterns the user (or, in
a later batch, PILK itself) has chosen to keep. It is deliberately
small and human-curated in this phase:

- Entries are created manually from the Memory surface.
- Each entry has a `kind`, a short `title`, and a plain-text `body`.
- Deletes are one-at-a-time or clear-all; no editing, no ranking.

Vector/semantic memory and auto-extraction from conversations are
out of scope here and live on the phase roadmap.
"""

from core.memory.hydration import HydratedContext, extract_topics, hydrate
from core.memory.store import MemoryEntry, MemoryKind, MemoryStore

__all__ = [
    "HydratedContext",
    "MemoryEntry",
    "MemoryKind",
    "MemoryStore",
    "extract_topics",
    "hydrate",
]
