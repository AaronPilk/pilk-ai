"""Unit tests for the Google Ads REST client.

Network stubbed via ``httpx.MockTransport``. Covers:

- customer_resource normalisation
- OAuth refresh-token dance (request shape + caching)
- OAuth error surfacing
- searchStream result flattening (chunk list → row list)
- Every create endpoint, asserting PAUSED is always sent
- set_campaign_status + update_campaign_budget
- Upstream error surfacing via GoogleAdsError (flat + nested details)
- Responsive search ad count validation (3-15 headlines, 2-4 descriptions)
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx
import pytest

from core.integrations.google_ads import (
    GoogleAdsClient,
    GoogleAdsConfig,
    GoogleAdsError,
)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    login_customer_id: str | None = None,
) -> GoogleAdsClient:
    cfg = GoogleAdsConfig(
        developer_token="dev-token-abc",
        client_id="cli-id",
        client_secret="cli-secret",
        refresh_token="refresh-xyz",
        customer_id="1234567890",
        login_customer_id=login_customer_id,
    )
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    return GoogleAdsClient(config=cfg)


@pytest.fixture(autouse=True)
def _restore_httpx_init():
    original = httpx.AsyncClient.__init__
    yield
    httpx.AsyncClient.__init__ = original  # type: ignore[method-assign]


def _ok_oauth(expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": "access-fresh", "expires_in": expires_in},
    )


# ── config + auth ────────────────────────────────────────────────


def test_customer_resource_strips_dashes() -> None:
    cfg = GoogleAdsConfig(
        developer_token="d",
        client_id="c",
        client_secret="s",
        refresh_token="r",
        customer_id="123-456-7890",
    )
    assert cfg.customer_resource == "customers/1234567890"


@pytest.mark.asyncio
async def test_oauth_refresh_is_sent_once_and_cached() -> None:
    calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append({"url": str(req.url), "method": req.method})
        if "oauth2.googleapis.com/token" in str(req.url):
            return _ok_oauth()
        return httpx.Response(200, json=[{"results": []}])

    client = _client(handler)
    await client.search("SELECT campaign.id FROM campaign")
    await client.search("SELECT campaign.id FROM campaign")
    oauth_calls = [c for c in calls if "oauth2" in c["url"]]
    # Exactly one refresh for two searches — token caching works.
    assert len(oauth_calls) == 1


@pytest.mark.asyncio
async def test_oauth_error_surfaces_as_google_ads_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Token revoked",
            },
        )

    client = _client(handler)
    with pytest.raises(GoogleAdsError) as exc:
        await client.search("SELECT campaign.id FROM campaign")
    assert exc.value.status == 400
    assert "Token revoked" in exc.value.message


@pytest.mark.asyncio
async def test_login_customer_id_header_sent_when_set() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if "oauth2" in str(req.url):
            return _ok_oauth()
        return httpx.Response(200, json=[{"results": []}])

    client = _client(handler, login_customer_id="999-888-7777")
    await client.search("SELECT campaign.id FROM campaign")
    api_reqs = [r for r in captured if "googleads" in str(r.url)]
    assert api_reqs[0].headers.get("login-customer-id") == "9998887777"


@pytest.mark.asyncio
async def test_no_login_customer_id_header_when_unset() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if "oauth2" in str(req.url):
            return _ok_oauth()
        return httpx.Response(200, json=[{"results": []}])

    client = _client(handler)
    await client.search("SELECT campaign.id FROM campaign")
    api_reqs = [r for r in captured if "googleads" in str(r.url)]
    assert "login-customer-id" not in api_reqs[0].headers


# ── search / reporting ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_flattens_chunks_into_single_list() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth()
        return httpx.Response(
            200,
            json=[
                {"results": [{"campaign": {"id": "1"}}]},
                {"results": [{"campaign": {"id": "2"}}]},
                {"results": []},
            ],
        )

    client = _client(handler)
    rows = await client.search("SELECT campaign.id FROM campaign")
    assert [r["campaign"]["id"] for r in rows] == ["1", "2"]


@pytest.mark.asyncio
async def test_search_accepts_object_response() -> None:
    """Non-streamed responses come back as a single dict, not a list."""

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth()
        return httpx.Response(
            200,
            json={"results": [{"campaign": {"id": "5"}}]},
        )

    client = _client(handler)
    rows = await client.search("SELECT campaign.id FROM campaign")
    assert rows[0]["campaign"]["id"] == "5"


# ── mutations are always PAUSED ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_campaign_always_sends_paused() -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth()
        body = json.loads(req.content.decode()) if req.content else {}
        captured.append(body)
        return httpx.Response(
            200,
            json={"results": [{"resourceName": "customers/1234567890/campaigns/99"}]},
        )

    client = _client(handler)
    await client.create_campaign(
        name="Test",
        budget_resource="customers/1234567890/campaignBudgets/1",
    )
    assert captured[0]["operations"][0]["create"]["status"] == "PAUSED"


@pytest.mark.asyncio
async def test_create_ad_group_always_sends_paused() -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth()
        captured.append(json.loads(req.content.decode()))
        return httpx.Response(
            200,
            json={"results": [{"resourceName": "x"}]},
        )

    client = _client(handler)
    await client.create_ad_group(
        name="Group A",
        campaign_resource="customers/1234567890/campaigns/99",
    )
    assert captured[0]["operations"][0]["create"]["status"] == "PAUSED"


@pytest.mark.asyncio
async def test_create_responsive_search_ad_always_sends_paused() -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth()
        captured.append(json.loads(req.content.decode()))
        return httpx.Response(200, json={"results": [{"resourceName": "x"}]})

    client = _client(handler)
    await client.create_responsive_search_ad(
        ad_group_resource="customers/1234567890/adGroups/1",
        headlines=["Headline 1", "Headline 2", "Headline 3"],
        descriptions=["Description one body", "Description two body"],
        final_urls=["https://example.com"],
    )
    assert captured[0]["operations"][0]["create"]["status"] == "PAUSED"


@pytest.mark.asyncio
async def test_rsa_rejects_too_few_headlines() -> None:
    client = _client(lambda _r: _ok_oauth())
    with pytest.raises(GoogleAdsError) as exc:
        await client.create_responsive_search_ad(
            ad_group_resource="customers/1234567890/adGroups/1",
            headlines=["Only one"],
            descriptions=["Desc one", "Desc two"],
            final_urls=["https://example.com"],
        )
    assert "3-15 headlines" in exc.value.message


@pytest.mark.asyncio
async def test_rsa_rejects_too_few_descriptions() -> None:
    client = _client(lambda _r: _ok_oauth())
    with pytest.raises(GoogleAdsError) as exc:
        await client.create_responsive_search_ad(
            ad_group_resource="customers/1234567890/adGroups/1",
            headlines=["A", "B", "C"],
            descriptions=["Only one description"],
            final_urls=["https://example.com"],
        )
    assert "2-4 descriptions" in exc.value.message


@pytest.mark.asyncio
async def test_create_campaign_rejects_unknown_bid_strategy() -> None:
    client = _client(lambda _r: _ok_oauth())
    with pytest.raises(GoogleAdsError) as exc:
        await client.create_campaign(
            name="x",
            budget_resource="y",
            bidding_strategy_type="TARGET_ROAS",
        )
    assert "bidding_strategy_type" in exc.value.message


# ── status + budget updates ──────────────────────────────────────


@pytest.mark.asyncio
async def test_set_campaign_status_rejects_invalid() -> None:
    client = _client(lambda _r: _ok_oauth())
    with pytest.raises(GoogleAdsError):
        await client.set_campaign_status(
            "customers/1234567890/campaigns/99", "ARCHIVED"
        )


@pytest.mark.asyncio
async def test_update_campaign_budget_uses_amount_micros() -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth()
        captured.append(json.loads(req.content.decode()))
        return httpx.Response(200, json={"results": [{"resourceName": "x"}]})

    client = _client(handler)
    await client.update_campaign_budget(
        "customers/1234567890/campaignBudgets/1",
        amount_micros=42_000_000,
    )
    op = captured[0]["operations"][0]
    assert op["update"]["amountMicros"] == "42000000"
    assert op["updateMask"] == "amount_micros"


# ── error surfacing ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deep_error_details_are_preferred() -> None:
    """Google nests the actionable message two levels deep; we should
    surface that instead of the generic outer message."""

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth()
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "generic fallback",
                    "details": [
                        {
                            "errors": [
                                {"message": "Specific: invalid customer id"}
                            ]
                        }
                    ],
                }
            },
        )

    client = _client(handler)
    with pytest.raises(GoogleAdsError) as exc:
        await client.search("SELECT campaign.id FROM campaign")
    assert "invalid customer id" in exc.value.message


@pytest.mark.asyncio
async def test_non_json_error_surfaces_cleanly() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth()
        return httpx.Response(502, text="upstream dead")

    client = _client(handler)
    with pytest.raises(GoogleAdsError) as exc:
        await client.search("SELECT campaign.id FROM campaign")
    assert exc.value.status == 502
    assert "upstream dead" in exc.value.message


# ── access-token expiry ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_access_token_triggers_refresh() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            calls.append("oauth")
            return _ok_oauth()
        return httpx.Response(200, json=[{"results": []}])

    client = _client(handler)
    await client.search("SELECT campaign.id FROM campaign")
    # Simulate token expiry by pushing the cached expiry into the past.
    client._access.expires_at = time.time() - 10
    await client.search("SELECT campaign.id FROM campaign")
    assert calls.count("oauth") == 2
