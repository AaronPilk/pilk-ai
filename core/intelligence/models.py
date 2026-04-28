"""Dataclasses + literals shared across the Intelligence Engine.

Kept separate from the service modules so HTTP routes, fetchers, and
the (future) scoring layer can all import the shapes without pulling
in the SQLite-bound services. Future code that adds embeddings or
the scoring backend imports from here too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Source kinds the engine knows how to fetch. ``manual`` is a
# special operator-curated kind: the daemon never polls it, and
# items only land via ``POST /intelligence/sources/<id>/items``
# (Batch 3C). All other kinds are network-fetched by the daemon /
# manual-refresh path; some are placeholders that the registry
# accepts (so an operator can pre-create a source) until their
# fetcher implementation lands.
SourceKind = Literal[
    "rss",
    "json_api",
    "html",
    "github_releases",
    "hacker_news",
    "arxiv",
    "youtube",
    "reddit",
    "x",
    "custom",
    "manual",
]

# Watchlist priorities. Drives default poll cadence + alert routing
# when those layers ship in Batches 4-5. For Batch 1 it's metadata
# only — no behaviour changes based on this value yet.
Priority = Literal["low", "medium", "high", "critical"]

# Item lifecycle. Batch 1 only ever sets ``new`` + ``stored``;
# ``scored`` / ``alerted`` / ``discarded`` arrive in later batches.
ItemStatus = Literal["new", "stored", "scored", "alerted", "discarded"]


@dataclass(frozen=True)
class SourceSpec:
    """Configured external source PILK watches.

    Identity: ``slug`` is the stable human-readable handle used in
    URLs and configs (e.g. ``anthropic-blog``, ``hn-frontpage``).
    ``id`` is the row UUID for foreign-key joins.
    """

    id: str
    slug: str
    kind: SourceKind
    label: str
    url: str
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    default_priority: Priority = "medium"
    project_slug: str | None = None
    poll_interval_seconds: int = 3600
    last_checked_at: str | None = None
    last_status: str | None = None
    consecutive_failures: int = 0
    etag: str | None = None
    last_modified: str | None = None
    mute_until: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class Topic:
    """Watchlist entry — a topic Aaron cares about.

    ``keywords`` drive the (future) keyword scorer. Empty keyword list
    is allowed in Batch 1 — the scorer hasn't shipped yet, so a topic
    is just metadata until then.
    """

    id: str
    slug: str
    label: str
    description: str = ""
    priority: Priority = "medium"
    project_slug: str | None = None
    keywords: list[str] = field(default_factory=list)
    mute_until: str | None = None
    created_at: str = ""
    updated_at: str = ""


__all__ = [
    "ItemStatus",
    "Priority",
    "SourceKind",
    "SourceSpec",
    "Topic",
]
