"""Shared types for source fetchers.

All fetchers return a :class:`FetchResult` so the manual-refresh
endpoint and (future) daemon can treat them uniformly. Errors raise
:class:`FetchError` (or its subclasses) — the caller is responsible
for recording the run outcome and the source's failure counter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FetchedItem:
    """Normalised representation of one item pulled from a source.

    All fetchers map their native shapes (RSS entry, HN story object,
    GitHub release JSON, etc.) into this dataclass. Downstream code
    (the item store, future scoring layer) deals only with this shape.
    """

    title: str
    url: str
    body: str = ""
    external_id: str | None = None
    published_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchResult:
    """Aggregate of a single fetch run from one source."""

    items: list[FetchedItem]
    etag: str | None = None
    last_modified: str | None = None
    fetched_url: str | None = None  # final URL after redirects, if known
    note: str | None = None         # for "not modified", "rate-limited", etc.


class FetchError(RuntimeError):
    """Raised when a fetch fails for a recoverable reason (network,
    timeout, parse). Caller should record the outcome + maybe retry
    later. Don't crash the daemon."""


class NotImplementedFetchError(FetchError):
    """Raised when a source's kind has no fetcher implementation yet.
    Distinguished from FetchError so the HTTP layer can return a
    clear 501 instead of a generic 500."""


__all__ = [
    "FetchError",
    "FetchResult",
    "FetchedItem",
    "NotImplementedFetchError",
]
