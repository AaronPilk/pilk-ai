"""Hacker News fetcher — top stories via the official Firebase API.

API surface used:
  - GET https://hacker-news.firebaseio.com/v0/topstories.json
       returns a JSON array of story IDs (~500 entries)
  - GET https://hacker-news.firebaseio.com/v0/item/{id}.json
       returns the story object

Source config (optional):
  ``{"limit": 30}`` — how many top stories to pull per refresh
  ``{"min_score": 100}`` — drop stories below this HN score

No API key required. The endpoints are public + rate-friendly. We
still cap concurrent item fetches and stop after ``limit`` for
politeness.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from core.intelligence.fetchers.base import (
    FetchError,
    FetchResult,
    FetchedItem,
)
from core.logging import get_logger

log = get_logger("pilkd.intelligence.fetchers.hn")

USER_AGENT = "PILK-Intelligence/0.2 (+intelligence-engine)"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
ITEM_FETCH_CONCURRENCY = 4


async def fetch_hacker_news(
    source,
    *,
    http: httpx.AsyncClient | None = None,
) -> FetchResult:
    config = source.config or {}
    try:
        limit = int(config.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))
    try:
        min_score = int(config.get("min_score") or 0)
    except (TypeError, ValueError):
        min_score = 0

    owns_client = http is None
    client = http or httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )

    try:
        try:
            top_resp = await client.get(
                "https://hacker-news.firebaseio.com/v0/topstories.json"
            )
        except httpx.HTTPError as e:
            raise FetchError(f"HN topstories network error: {e}") from e
        if top_resp.status_code != 200:
            raise FetchError(
                f"HN topstories HTTP {top_resp.status_code}"
            )
        try:
            ids = top_resp.json()
        except ValueError as e:
            raise FetchError(f"HN topstories not JSON: {e}") from e
        if not isinstance(ids, list):
            raise FetchError("HN topstories returned non-list payload")
        ids = [int(i) for i in ids[:limit] if isinstance(i, int)]

        # Bounded fan-out for item fetches — politeness over speed.
        sem = asyncio.Semaphore(ITEM_FETCH_CONCURRENCY)
        tasks = [
            _fetch_item(client, sem, story_id)
            for story_id in ids
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if owns_client:
            await client.aclose()

    items: list[FetchedItem] = []
    for r in results:
        if isinstance(r, Exception):
            log.warning("hn_item_fetch_failed", error=str(r))
            continue
        if r is None:
            continue
        story = r
        if min_score and (story.get("score") or 0) < min_score:
            continue
        item = _story_to_item(story)
        if item is not None:
            items.append(item)

    return FetchResult(items=items, fetched_url=source.url or "hn-topstories")


async def _fetch_item(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    story_id: int,
) -> dict[str, Any] | None:
    async with sem:
        try:
            r = await client.get(
                f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"item {story_id}: {e}") from e
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _story_to_item(story: dict[str, Any]) -> FetchedItem | None:
    if story.get("deleted") or story.get("dead"):
        return None
    title = (story.get("title") or "").strip()
    if not title:
        return None
    story_id = story.get("id")
    # HN stories have either a ``url`` (link post) or ``text`` (Ask
    # / Show HN). Prefer the linked URL; fall back to the HN
    # discussion page so dedup can still work.
    external_url = story.get("url")
    discussion_url = (
        f"https://news.ycombinator.com/item?id={story_id}"
        if story_id is not None
        else ""
    )
    item_url = external_url or discussion_url
    if not item_url:
        return None
    body = story.get("text") or ""
    score = story.get("score")
    by = story.get("by")
    descendants = story.get("descendants")
    published_iso: str | None = None
    ts = story.get("time")
    if isinstance(ts, (int, float)):
        try:
            published_iso = datetime.fromtimestamp(ts, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            published_iso = None
    raw = {
        "hn_id": story_id,
        "hn_score": score,
        "hn_by": by,
        "hn_descendants": descendants,
        "hn_type": story.get("type"),
        "discussion_url": discussion_url,
    }
    return FetchedItem(
        title=title,
        url=item_url,
        body=body,
        external_id=str(story_id) if story_id is not None else None,
        published_at=published_iso,
        raw=raw,
    )


__all__ = ["fetch_hacker_news"]
