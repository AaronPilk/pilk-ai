"""Unit tests for the Apify client.

Network calls are stubbed via ``httpx.MockTransport``. Coverage:

- URL slug normalisation (`/` → `~`) on run_actor
- Token query-param injection
- Happy-path dataset return for each high-level wrapper
- Hashtag + username sanitisation (# and @ stripped)
- Upstream error surfacing via ApifyError
- Non-JSON response surfacing
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from core.integrations.apify import ApifyClient, ApifyConfig, ApifyError


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> ApifyClient:
    cfg = ApifyConfig(api_token="tok-xyz")
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    return ApifyClient(cfg)


@pytest.fixture(autouse=True)
def _restore_httpx_init():
    original = httpx.AsyncClient.__init__
    yield
    httpx.AsyncClient.__init__ = original  # type: ignore[method-assign]


# ── run_actor ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_actor_slug_and_token() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["method"] = req.method
        return httpx.Response(200, json=[{"id": "row1"}])

    client = _client(handler)
    rows = await client.run_actor("apify/instagram-scraper", {"x": 1})
    assert rows == [{"id": "row1"}]
    assert "acts/apify~instagram-scraper/run-sync-get-dataset-items" in seen["url"]
    assert "token=tok-xyz" in seen["url"]
    assert seen["method"] == "POST"


@pytest.mark.asyncio
async def test_run_actor_surfaces_upstream_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "token invalid"}},
        )

    client = _client(handler)
    with pytest.raises(ApifyError) as exc:
        await client.run_actor("apify/foo", {})
    assert exc.value.status == 401
    assert "token invalid" in exc.value.message


@pytest.mark.asyncio
async def test_run_actor_surfaces_non_json_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="gateway timeout")

    client = _client(handler)
    with pytest.raises(ApifyError) as exc:
        await client.run_actor("apify/foo", {})
    assert exc.value.status == 500
    assert "gateway timeout" in exc.value.message


@pytest.mark.asyncio
async def test_run_actor_rejects_non_array_payload() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    client = _client(handler)
    with pytest.raises(ApifyError):
        await client.run_actor("apify/foo", {})


# ── instagram wrappers ────────────────────────────────────────


@pytest.mark.asyncio
async def test_instagram_search_by_hashtag_sanitises_hashtag() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json=[{"id": "p1"}])

    client = _client(handler)
    rows = await client.instagram_search_by_hashtag("#skincare", limit=25)
    assert rows == [{"id": "p1"}]
    # The '#' was stripped before sending.
    assert seen["body"]["hashtags"] == ["skincare"]
    assert seen["body"]["resultsLimit"] == 25


@pytest.mark.asyncio
async def test_instagram_search_requires_hashtag() -> None:
    client = _client(lambda _r: httpx.Response(200, json=[]))
    with pytest.raises(ApifyError):
        await client.instagram_search_by_hashtag("   ")


@pytest.mark.asyncio
async def test_instagram_profile_strips_at() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json=[{"username": "ghost"}])

    client = _client(handler)
    got = await client.instagram_profile("@ghost")
    assert got == {"username": "ghost"}
    assert seen["body"]["directUrls"] == ["https://www.instagram.com/ghost/"]


@pytest.mark.asyncio
async def test_instagram_profile_returns_none_on_empty_dataset() -> None:
    client = _client(lambda _r: httpx.Response(200, json=[]))
    got = await client.instagram_profile("ghost")
    assert got is None


# ── tiktok wrappers ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_tiktok_search_passes_hashtag_and_downloads_off() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(req.content.decode())
        return httpx.Response(200, json=[{"id": "v1"}])

    client = _client(handler)
    await client.tiktok_search_by_hashtag("dance", limit=30)
    assert seen["body"]["hashtags"] == ["dance"]
    assert seen["body"]["resultsPerPage"] == 30
    # Critical: never download videos by default — that's expensive
    # and we can always re-pull specific ones later.
    assert seen["body"]["shouldDownloadVideos"] is False
    assert seen["body"]["shouldDownloadCovers"] is False


@pytest.mark.asyncio
async def test_tiktok_profile_requires_username() -> None:
    client = _client(lambda _r: httpx.Response(200, json=[]))
    with pytest.raises(ApifyError):
        await client.tiktok_profile("")
