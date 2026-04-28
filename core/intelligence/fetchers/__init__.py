"""Source fetchers — one module per source kind.

Each fetcher exposes ``fetch(source, *, http) -> FetchResult`` and is
selected by the source's ``kind`` field. Batch 1 ships ``rss`` only;
the dispatch table here also registers explicit "not implemented yet"
stubs for the other declared kinds so an operator who pre-creates a
JSON-API source gets a clean error message instead of a stack trace.
"""

from __future__ import annotations

from core.intelligence.fetchers.arxiv import fetch_arxiv
from core.intelligence.fetchers.base import (
    FetchedItem,
    FetchResult,
    NotImplementedFetchError,
)
from core.intelligence.fetchers.github_releases import fetch_github_releases
from core.intelligence.fetchers.hacker_news import fetch_hacker_news
from core.intelligence.fetchers.rss import fetch_rss

__all__ = [
    "FetchResult",
    "FetchedItem",
    "NotImplementedFetchError",
    "fetch_arxiv",
    "fetch_for_source",
    "fetch_github_releases",
    "fetch_hacker_news",
    "fetch_rss",
]


async def fetch_for_source(source, *, http=None) -> FetchResult:
    """Dispatch a fetch call based on ``source.kind``. Lives here
    rather than in the daemon so HTTP routes can call it directly
    for the manual-refresh endpoint.

    Batch 2/3C supports network-fetched: ``rss``, ``hacker_news``,
    ``github_releases``, ``arxiv``. The ``manual`` kind is
    operator-curated — items land via
    ``POST /intelligence/sources/<id>/items``; the dispatcher
    refuses to fetch it so an accidental ``/refresh`` doesn't
    silently no-op or scrape. Other kinds (json_api, html, youtube,
    reddit, x, custom) raise ``NotImplementedFetchError`` so the
    operator gets a clear 501 instead of a stack trace.
    """
    kind = source.kind
    if kind == "rss":
        return await fetch_rss(source, http=http)
    if kind == "hacker_news":
        return await fetch_hacker_news(source, http=http)
    if kind == "github_releases":
        return await fetch_github_releases(source, http=http)
    if kind == "arxiv":
        return await fetch_arxiv(source, http=http)
    if kind == "manual":
        raise NotImplementedFetchError(
            "Manual sources are operator-curated; they don't accept "
            "/refresh. Push items in via POST "
            "/intelligence/sources/<id>/items instead."
        )
    raise NotImplementedFetchError(
        f"Source kind '{kind}' is registered but its fetcher hasn't "
        "been implemented yet. Available now: rss, hacker_news, "
        "github_releases, arxiv (auto-fetched), manual (operator-"
        "submitted). Other kinds (json_api, html, youtube, reddit, "
        "x, custom) land in later batches."
    )
