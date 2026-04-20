"""Apify client — thin wrapper over the run-sync-get-dataset-items API.

Apify is a marketplace of scraper "actors" (hosted scrapers) that each
take a JSON input and emit a JSON dataset. The ugc_scout_agent drives
three actors through this client:

    apify/instagram-scraper           IG profile + post pull
    clockworks/tiktok-scraper         TikTok profile + post pull
    apify/instagram-hashtag-scraper   hashtag-based discovery

We deliberately stay small: one synchronous "run the actor, wait,
return the dataset" call via ``run-sync-get-dataset-items``. No webhook
plumbing, no paginated dataset walks — Apify caps the sync call at
5 minutes which is more than enough for the creator-discovery batches
we run (typically a few hundred records).

Docs: https://docs.apify.com/api/v2
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from core.logging import get_logger

log = get_logger("pilkd.apify")

APIFY_API_BASE = "https://api.apify.com/v2"
DEFAULT_TIMEOUT_S = 300.0  # Apify's own sync-run ceiling
DEFAULT_RESULT_LIMIT = 100


class ApifyError(Exception):
    """Apify returned non-2xx or the actor run ended in a failure state."""

    def __init__(self, status: int, message: str, raw: Any = None):
        super().__init__(f"Apify {status}: {message}")
        self.status = status
        self.message = message
        self.raw = raw


@dataclass(frozen=True)
class ApifyConfig:
    api_token: str
    api_base: str = APIFY_API_BASE


class ApifyClient:
    """Run an actor synchronously and return its dataset items.

    Most callers should prefer the high-level wrappers on this class
    (``instagram_search_by_hashtag``, ``tiktok_search_by_hashtag``,
    ``instagram_profile``, etc.) rather than reaching for
    :meth:`run_actor` directly — the wrappers know each actor's input
    shape and normalise the output.
    """

    def __init__(self, config: ApifyConfig, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._cfg = config
        self._timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self._cfg.api_base}/{path.lstrip('/')}"

    async def run_actor(
        self,
        actor_id: str,
        actor_input: dict[str, Any],
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Run ``actor_id`` (slug form ``user/name``) with the given
        JSON input, wait for it to finish, and return its dataset items.

        Apify accepts the slug with `/` replaced by `~` in the URL.
        """
        slug = actor_id.replace("/", "~")
        params: dict[str, Any] = {"token": self._cfg.api_token}
        if limit is not None:
            params["limit"] = int(limit)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                self._url(f"acts/{slug}/run-sync-get-dataset-items"),
                params=params,
                json=actor_input,
            )
        if not r.is_success:
            try:
                body = r.json()
                err = body.get("error") or {}
                message = str(err.get("message") or r.text[:200])
            except ValueError:
                message = r.text[:200] or f"HTTP {r.status_code}"
                body = None
            raise ApifyError(status=r.status_code, message=message, raw=body)
        try:
            data = r.json()
        except ValueError as e:
            raise ApifyError(
                status=r.status_code,
                message="Apify returned non-JSON on a successful run",
            ) from e
        if not isinstance(data, list):
            raise ApifyError(
                status=r.status_code,
                message="Apify dataset response was not a JSON array",
                raw=data,
            )
        return data

    # ── High-level wrappers ────────────────────────────────────────

    async def instagram_search_by_hashtag(
        self,
        hashtag: str,
        *,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Return recent IG posts for a hashtag. Each item has the
        poster's username, caption, mediaType, likes, comments, and a
        display URL. The ugc tool turns these into unique creators."""
        hashtag_clean = hashtag.lstrip("#").strip()
        if not hashtag_clean:
            raise ApifyError(status=400, message="hashtag is required")
        return await self.run_actor(
            "apify/instagram-hashtag-scraper",
            {
                "hashtags": [hashtag_clean],
                "resultsLimit": int(limit),
            },
        )

    async def instagram_profile(
        self,
        username: str,
        *,
        post_limit: int = 12,
    ) -> dict[str, Any] | None:
        """Return the full profile payload for one IG username plus up
        to ``post_limit`` recent posts. Returns None if Apify emits an
        empty dataset (deleted / private account)."""
        uname = username.lstrip("@").strip()
        if not uname:
            raise ApifyError(status=400, message="username is required")
        items = await self.run_actor(
            "apify/instagram-scraper",
            {
                "directUrls": [f"https://www.instagram.com/{uname}/"],
                "resultsType": "details",
                "resultsLimit": int(post_limit),
                "searchType": "user",
                "searchLimit": 1,
            },
        )
        return items[0] if items else None

    async def tiktok_search_by_hashtag(
        self,
        hashtag: str,
        *,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Return recent TikTok videos for a hashtag. Each item carries
        authorMeta, text, playCount, shareCount, diggCount, and a
        downloadable videoUrl for later vision-scoring."""
        hashtag_clean = hashtag.lstrip("#").strip()
        if not hashtag_clean:
            raise ApifyError(status=400, message="hashtag is required")
        return await self.run_actor(
            "clockworks/tiktok-scraper",
            {
                "hashtags": [hashtag_clean],
                "resultsPerPage": int(limit),
                "shouldDownloadVideos": False,
                "shouldDownloadCovers": False,
            },
        )

    async def tiktok_profile(
        self,
        username: str,
        *,
        post_limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Return the most recent ``post_limit`` TikToks for a
        username. Profile metadata (follower count, bio) lives on each
        item under ``authorMeta``."""
        uname = username.lstrip("@").strip()
        if not uname:
            raise ApifyError(status=400, message="username is required")
        return await self.run_actor(
            "clockworks/tiktok-scraper",
            {
                "profiles": [uname],
                "resultsPerPage": int(post_limit),
                "shouldDownloadVideos": False,
                "shouldDownloadCovers": False,
            },
        )


__all__ = ["ApifyClient", "ApifyConfig", "ApifyError"]
