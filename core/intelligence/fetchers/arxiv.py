"""arXiv fetcher — Atom feed via the public ``export.arxiv.org`` API.

Operator-friendly: instead of writing the full query URL, the
operator can provide just the search params via config:

  ``{"search_query": "cat:cs.AI", "max_results": 20}``

If ``search_query`` is omitted, ``source.url`` is used verbatim
(so an operator can paste a constructed URL too — both paths work).

Wraps the existing RSS/Atom parser since arXiv emits Atom 1.0 with
the standard fields. We just construct the URL + delegate.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from core.intelligence.fetchers.base import FetchError, FetchResult
from core.intelligence.fetchers.rss import fetch_rss

DEFAULT_MAX_RESULTS = 20
MAX_MAX_RESULTS = 100
DEFAULT_SORT_BY = "submittedDate"
DEFAULT_SORT_ORDER = "descending"


async def fetch_arxiv(
    source,
    *,
    http: httpx.AsyncClient | None = None,
) -> FetchResult:
    """Construct the arXiv Atom URL from config and delegate to the
    RSS/Atom fetcher. Reuses the same User-Agent + size caps + 304
    handling so we don't fork the polite-fetch logic.
    """
    config: dict[str, Any] = source.config or {}
    search_query = (config.get("search_query") or "").strip()

    if search_query:
        try:
            max_results = int(
                config.get("max_results") or DEFAULT_MAX_RESULTS
            )
        except (TypeError, ValueError):
            max_results = DEFAULT_MAX_RESULTS
        max_results = max(1, min(max_results, MAX_MAX_RESULTS))
        params = {
            "search_query": search_query,
            "max_results": max_results,
            "sortBy": config.get("sort_by") or DEFAULT_SORT_BY,
            "sortOrder": config.get("sort_order") or DEFAULT_SORT_ORDER,
        }
        url = "http://export.arxiv.org/api/query?" + urlencode(params)
    else:
        url = (source.url or "").strip()
        if not url:
            raise FetchError(
                "arXiv source requires either config.search_query "
                "(e.g. 'cat:cs.AI') or a fully-formed arXiv API URL "
                "in the url field."
            )

    # Wrap the source so the RSS fetcher uses the constructed URL
    # without mutating the persisted ``source`` row.
    proxy = _ArxivSourceProxy(source, url=url)
    return await fetch_rss(proxy, http=http)


class _ArxivSourceProxy:
    """Lightweight wrapper that forwards every attribute to the real
    source except the ``url`` field, which we override with the
    constructed query URL. Avoids mutating the live source row."""

    __slots__ = ("_inner", "url")

    def __init__(self, inner: Any, *, url: str) -> None:
        self._inner = inner
        self.url = url

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


__all__ = ["fetch_arxiv"]
