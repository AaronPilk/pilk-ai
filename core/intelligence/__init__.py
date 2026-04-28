"""PILK Intelligence Engine — Batch 1 foundation.

The Intelligence Engine watches external information sources (RSS,
news, blogs, GitHub releases, arXiv, Hacker News, etc.) and stores
deduplicated items in SQLite + the brain vault. It is **source-
agnostic** — Aaron registers sources and watchlist topics; the engine
doesn't care whether the topic is "AI agents," "gold price," or
"climbing gear."

Batch 1 scope (this file's purpose):
  - Source registry — operator-defined feeds + URLs to watch
  - Watchlist topics — keyword-driven priorities for relevance
  - Item store — deduplicated fetched items
  - RSS fetcher — single source kind for v1

Explicitly OUT of Batch 1:
  - Background daemon (manual refresh only)
  - Telegram / dashboard alerts
  - LLM scoring
  - Vector embeddings
  - Other source kinds (HN, GitHub, arXiv, HTML scraping)

Default behaviour: every new source is created with ``enabled=true``
but no daemon polls them — items only land when the operator hits
``POST /intelligence/sources/{id}/refresh`` or the test endpoint.
This keeps Batch 1 stability-safe: nothing autonomous, nothing
spending money, nothing alerting Aaron.
"""

from core.intelligence.dedup import canonical_url, content_hash
from core.intelligence.items import ItemStore, IntelItem
from core.intelligence.models import (
    SourceSpec,
    Topic,
    SourceKind,
    Priority,
    ItemStatus,
)
from core.intelligence.sources import SourceRegistry
from core.intelligence.topics import TopicRegistry

__all__ = [
    "IntelItem",
    "ItemStatus",
    "ItemStore",
    "Priority",
    "SourceKind",
    "SourceRegistry",
    "SourceSpec",
    "Topic",
    "TopicRegistry",
    "canonical_url",
    "content_hash",
]
