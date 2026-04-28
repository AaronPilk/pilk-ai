"""GitHub releases fetcher — public REST API.

Source URL: ``https://github.com/<owner>/<repo>`` (the human URL —
we parse owner/repo from it).

Or via config (overrides URL parsing):
  ``{"owner": "...", "repo": "..."}``
  ``{"limit": 10}``                 (default 10, max 50)
  ``{"include_prereleases": false}`` (default false)

API:
  GET https://api.github.com/repos/{owner}/{repo}/releases?per_page=N

Auth: optional. Without auth, GitHub allows 60 req/hr per IP — fine
for the daemon's polite cadence (one source-fetch per poll
interval). Operators with a PAT can drop it into a future
``intel_settings`` table; Batch 2 stays anonymous.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from core.intelligence.fetchers.base import (
    FetchError,
    FetchResult,
    FetchedItem,
)
from core.logging import get_logger

log = get_logger("pilkd.intelligence.fetchers.github")

USER_AGENT = "PILK-Intelligence/0.2 (+intelligence-engine)"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_LIMIT = 10
MAX_LIMIT = 50

_REPO_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9._-]+)/"
    r"([A-Za-z0-9._-]+?)(?:/.*)?/?$",
    re.IGNORECASE,
)


async def fetch_github_releases(
    source,
    *,
    http: httpx.AsyncClient | None = None,
) -> FetchResult:
    owner, repo = _resolve_owner_repo(source)
    config = source.config or {}
    try:
        limit = int(config.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))
    include_pre = bool(config.get("include_prereleases", False))

    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if source.etag:
        headers["If-None-Match"] = source.etag

    api_url = (
        f"https://api.github.com/repos/{owner}/{repo}/releases"
        f"?per_page={limit}"
    )

    owns_client = http is None
    client = http or httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT_S,
        follow_redirects=True,
    )
    try:
        try:
            resp = await client.get(api_url, headers=headers)
        except httpx.HTTPError as e:
            raise FetchError(f"GitHub network error: {e}") from e

        if resp.status_code == 304:
            return FetchResult(
                items=[],
                etag=source.etag,
                fetched_url=api_url,
                note="not modified (304)",
            )
        if resp.status_code == 404:
            raise FetchError(
                f"repo {owner}/{repo} not found (404). The repo may "
                "be private or moved."
            )
        if resp.status_code == 403:
            # Likely rate limit hit. Surface the reset hint if any.
            reset = resp.headers.get("X-RateLimit-Reset")
            raise FetchError(
                "GitHub rate-limited (403). Without an auth token, "
                "the limit is 60 requests/hour per IP. Slow the "
                "source's poll_interval_seconds or add a PAT in a "
                f"future settings batch. Reset epoch: {reset or '?'}"
            )
        if resp.status_code != 200:
            raise FetchError(
                f"GitHub HTTP {resp.status_code}: "
                f"{(resp.text or '')[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise FetchError(f"GitHub returned non-JSON: {e}") from e
        if not isinstance(payload, list):
            raise FetchError("GitHub releases payload was not a list")

        items: list[FetchedItem] = []
        for rel in payload:
            if not isinstance(rel, dict):
                continue
            if rel.get("draft"):
                continue
            if rel.get("prerelease") and not include_pre:
                continue
            item = _release_to_item(owner, repo, rel)
            if item is not None:
                items.append(item)

        etag = resp.headers.get("ETag") or resp.headers.get("etag")
        return FetchResult(
            items=items,
            etag=etag,
            fetched_url=api_url,
        )
    finally:
        if owns_client:
            await client.aclose()


def _resolve_owner_repo(source) -> tuple[str, str]:
    config = source.config or {}
    owner = (config.get("owner") or "").strip()
    repo = (config.get("repo") or "").strip()
    if owner and repo:
        return owner, repo
    m = _REPO_URL_RE.match((source.url or "").strip())
    if not m:
        raise FetchError(
            "GitHub source needs a github.com/owner/repo URL or "
            "config={'owner','repo'}. Got URL: " + (source.url or "")
        )
    return m.group(1), m.group(2)


def _release_to_item(
    owner: str, repo: str, rel: dict[str, Any],
) -> FetchedItem | None:
    tag = rel.get("tag_name") or rel.get("name") or ""
    name = rel.get("name") or tag
    if not tag and not name:
        return None
    title = f"{owner}/{repo} — {name}".strip(" —")
    html_url = rel.get("html_url") or (
        f"https://github.com/{owner}/{repo}/releases/tag/{tag}"
    )
    body = rel.get("body") or ""
    published = rel.get("published_at") or rel.get("created_at")
    # Normalise to ISO 8601 with timezone — GitHub already returns
    # ISO strings, but tolerate occasional malformed values.
    published_iso: str | None = None
    if isinstance(published, str) and published:
        try:
            datetime.fromisoformat(published.replace("Z", "+00:00"))
            published_iso = published
        except ValueError:
            published_iso = None

    return FetchedItem(
        title=title,
        url=html_url,
        body=body,
        external_id=str(rel.get("id") or tag),
        published_at=published_iso,
        raw={
            "github_owner": owner,
            "github_repo": repo,
            "tag_name": tag,
            "prerelease": bool(rel.get("prerelease")),
            "author": (rel.get("author") or {}).get("login"),
        },
    )


__all__ = ["fetch_github_releases"]
