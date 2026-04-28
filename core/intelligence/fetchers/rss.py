"""RSS / Atom fetcher.

Pulls a feed URL with ``httpx`` (so ETags + If-Modified-Since work
identically across sources), parses with ``feedparser`` (handles
RSS 0.9x/2.0 + Atom + iTunes extensions), and emits normalised
:class:`FetchedItem` rows.

Batch 1 design constraints:
  - Be polite (User-Agent, follow redirects, honour 304 Not Modified)
  - Bound the response size (1 MiB cap — bigger feeds need a
    streaming parser we'll add later)
  - Bound the per-fetch timeout (15s)
  - Bound the per-fetch item count (250 — feeds bigger than that get
    truncated to the newest 250 entries)
  - Fail soft: malformed entries get logged + skipped, not raised
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from core.intelligence.fetchers.base import (
    FetchError,
    FetchResult,
    FetchedItem,
)
from core.logging import get_logger

log = get_logger("pilkd.intelligence.fetchers.rss")

# Conservative limits — better to throttle than to surprise an
# external host with a flood of requests.
USER_AGENT = "PILK-Intelligence/0.1 (+https://github.com/pilksclaes/pilk)"
DEFAULT_TIMEOUT_S = 15.0
MAX_BODY_BYTES = 1 * 1024 * 1024    # 1 MiB
MAX_ITEMS_PER_FEED = 250


async def fetch_rss(
    source,
    *,
    http: httpx.AsyncClient | None = None,
) -> FetchResult:
    """Fetch + parse one RSS / Atom source. ``http`` is injectable
    for testing; the production caller leaves it None and we open a
    short-lived client per call.
    """
    headers: dict[str, str] = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/rss+xml, application/atom+xml, "
            "application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
        ),
    }
    if source.etag:
        headers["If-None-Match"] = source.etag
    if source.last_modified:
        headers["If-Modified-Since"] = source.last_modified

    owns_client = http is None
    client = http or httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_S,
        follow_redirects=True,
    )
    try:
        try:
            resp = await client.get(source.url, headers=headers)
        except httpx.HTTPError as e:
            raise FetchError(f"network error: {e}") from e

        if resp.status_code == 304:
            log.info(
                "rss_not_modified",
                source_id=source.id,
                url=source.url,
            )
            return FetchResult(
                items=[],
                etag=source.etag,
                last_modified=source.last_modified,
                fetched_url=str(resp.url),
                note="not modified (304)",
            )

        if resp.status_code != 200:
            raise FetchError(
                f"HTTP {resp.status_code} from {source.url}: "
                f"{(resp.text or '')[:200]}"
            )

        body = resp.content
        if len(body) > MAX_BODY_BYTES:
            raise FetchError(
                f"feed body {len(body)} bytes exceeds cap "
                f"({MAX_BODY_BYTES})"
            )
    finally:
        if owns_client:
            await client.aclose()

    items = _parse_feed(body, source_url=str(resp.url))
    if len(items) > MAX_ITEMS_PER_FEED:
        items = items[:MAX_ITEMS_PER_FEED]

    return FetchResult(
        items=items,
        etag=resp.headers.get("ETag") or resp.headers.get("etag"),
        last_modified=resp.headers.get("Last-Modified")
        or resp.headers.get("last-modified"),
        fetched_url=str(resp.url),
    )


def _parse_feed(body: bytes, *, source_url: str) -> list[FetchedItem]:
    # ``feedparser`` is happy to consume bytes directly — no need to
    # guess the encoding ourselves.
    import feedparser  # local import: heavy, avoid at module load.

    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        # ``bozo`` means malformed feed; if there are no entries
        # surfaceable, treat as a fetch error so the source's
        # consecutive_failures counter ticks up.
        reason = getattr(parsed, "bozo_exception", None)
        raise FetchError(
            f"feed parse failed for {source_url}: {reason}"
        )

    out: list[FetchedItem] = []
    for entry in parsed.entries:
        try:
            item = _entry_to_item(entry)
        except Exception as e:  # noqa: BLE001 — defensive
            log.warning(
                "rss_entry_parse_skipped",
                source_url=source_url,
                error=str(e),
            )
            continue
        out.append(item)
    return out


def _entry_to_item(entry: Any) -> FetchedItem:
    title = (entry.get("title") or "").strip() or "(untitled)"
    url = (entry.get("link") or "").strip()
    if not url:
        raise ValueError("entry has no link")

    body_parts: list[str] = []
    summary = entry.get("summary") or entry.get("description") or ""
    if summary:
        body_parts.append(str(summary))
    content_blocks = entry.get("content") or []
    if isinstance(content_blocks, list):
        for cb in content_blocks:
            if isinstance(cb, dict):
                v = cb.get("value")
                if v:
                    body_parts.append(str(v))
    body = "\n\n".join(body_parts)

    external_id = (
        entry.get("id")
        or entry.get("guid")
        or entry.get("link")
        or None
    )

    published_at = _entry_published(entry)

    raw: dict[str, Any] = {
        "feedparser_id": entry.get("id"),
        "guid": entry.get("guid"),
        "author": entry.get("author"),
        "tags": [
            t.get("term")
            for t in (entry.get("tags") or [])
            if isinstance(t, dict) and t.get("term")
        ],
    }
    return FetchedItem(
        title=title,
        url=url,
        body=body,
        external_id=str(external_id) if external_id else None,
        published_at=published_at,
        raw=raw,
    )


def _entry_published(entry: Any) -> str | None:
    """Best-effort ISO 8601 timestamp extraction. Feeds vary wildly
    in how they format dates — we try the structured time tuple
    fields feedparser already parsed before falling back to the raw
    string fields."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        tup = entry.get(key)
        if tup is None:
            continue
        try:
            ts = time.mktime(tup)
            return datetime.fromtimestamp(ts, tz=UTC).isoformat()
        except (TypeError, ValueError, OverflowError):
            continue
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


__all__ = ["fetch_rss"]
