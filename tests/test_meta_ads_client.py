"""Unit tests for the Meta Marketing API client.

Network calls are stubbed via ``httpx.MockTransport``. We cover:

- account_node prefix normalisation
- every read endpoint (list_campaigns / adsets / ads / insights)
- every create endpoint, asserting status=PAUSED is always sent
- image + video upload, returning hash / id
- set_status + update_budget
- upstream error surfacing via MetaAdsError
- page_id enforcement on create_creative
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from core.integrations.meta_ads import (
    MetaAdsClient,
    MetaAdsConfig,
    MetaAdsError,
)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    page_id: str | None = None,
) -> MetaAdsClient:
    cfg = MetaAdsConfig(
        access_token="tok-123",
        ad_account_id="12345",
        page_id=page_id,
    )
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    # The MetaAdsClient constructs fresh httpx clients per call, so we
    # monkey-patch the constructor instead of injecting one client.
    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    return MetaAdsClient(cfg)


@pytest.fixture(autouse=True)
def _restore_httpx_init():
    """Reset httpx.AsyncClient.__init__ after each test — we're
    swapping it by attribute, not monkeypatch.setattr."""
    original = httpx.AsyncClient.__init__
    yield
    httpx.AsyncClient.__init__ = original  # type: ignore[method-assign]


# ── config normalisation ────────────────────────────────────────


def test_account_node_prefixes_act() -> None:
    cfg = MetaAdsConfig(access_token="t", ad_account_id="12345")
    assert cfg.account_node == "act_12345"


def test_account_node_preserves_existing_prefix() -> None:
    cfg = MetaAdsConfig(access_token="t", ad_account_id="act_12345")
    assert cfg.account_node == "act_12345"


# ── reads ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_campaigns_happy_path() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["method"] = req.method
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "c1",
                        "name": "Test",
                        "objective": "OUTCOME_TRAFFIC",
                        "effective_status": "PAUSED",
                    }
                ]
            },
        )

    client = _client(handler)
    rows = await client.list_campaigns()
    assert rows[0]["id"] == "c1"
    assert "act_12345/campaigns" in seen["url"]
    assert "access_token=tok-123" in seen["url"]
    assert seen["method"] == "GET"


@pytest.mark.asyncio
async def test_list_campaigns_filter_status() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"data": []})

    client = _client(handler)
    await client.list_campaigns(status="ACTIVE", limit=10)
    assert "effective_status" in captured["url"]
    assert "ACTIVE" in captured["url"]


@pytest.mark.asyncio
async def test_list_adsets_scoped_to_campaign() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"data": [{"id": "as1"}]})

    client = _client(handler)
    rows = await client.list_adsets(campaign_id="c1")
    assert rows[0]["id"] == "as1"
    assert "/c1/adsets" in seen["url"]


@pytest.mark.asyncio
async def test_list_adsets_unscoped_hits_account() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"data": []})

    client = _client(handler)
    await client.list_adsets()
    assert "act_12345/adsets" in seen["url"]


@pytest.mark.asyncio
async def test_list_ads_scoped_to_adset() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"data": []})

    client = _client(handler)
    await client.list_ads(adset_id="as1")
    assert "/as1/ads" in seen["url"]


@pytest.mark.asyncio
async def test_get_insights_defaults() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"impressions": "1000", "clicks": "12", "spend": "4.50"}
                ]
            },
        )

    client = _client(handler)
    rows = await client.get_insights("c1")
    assert rows[0]["clicks"] == "12"
    assert "/c1/insights" in seen["url"]
    assert "level=ad" in seen["url"]
    assert "last_7d" in seen["url"]


# ── creates (all PAUSED) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_campaign_forces_paused() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"id": "c-new"})

    client = _client(handler)
    out = await client.create_campaign(
        name="New Test",
        objective="OUTCOME_TRAFFIC",
        daily_budget=2000,
    )
    assert out["id"] == "c-new"
    assert "status=PAUSED" in seen["body"]
    assert "daily_budget=2000" in seen["body"]
    assert "OUTCOME_TRAFFIC" in seen["body"]


@pytest.mark.asyncio
async def test_create_adset_forces_paused_with_default_targeting() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"id": "as-new"})

    client = _client(handler)
    await client.create_adset(
        campaign_id="c1",
        name="AS",
        optimization_goal="LINK_CLICKS",
        billing_event="IMPRESSIONS",
        daily_budget=1500,
    )
    assert "status=PAUSED" in seen["body"]
    assert "LINK_CLICKS" in seen["body"]
    # Default geo_locations.countries=["US"] must be sent.
    assert "geo_locations" in seen["body"]
    assert "US" in seen["body"]


@pytest.mark.asyncio
async def test_create_ad_forces_paused_with_creative() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"id": "ad-new"})

    client = _client(handler)
    out = await client.create_ad(
        adset_id="as1", name="Ad", creative_id="cr1"
    )
    assert out["id"] == "ad-new"
    assert "status=PAUSED" in seen["body"]
    assert "creative_id" in seen["body"]
    assert "cr1" in seen["body"]


# ── creative: page_id enforcement ───────────────────────────────


@pytest.mark.asyncio
async def test_create_creative_requires_page_id() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(handler)  # page_id=None on config
    with pytest.raises(MetaAdsError) as ei:
        await client.create_creative(name="c", image_hash="h")
    assert "page_id" in ei.value.message


@pytest.mark.asyncio
async def test_create_creative_uses_config_page_id() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"id": "cr1"})

    client = _client(handler, page_id="page-999")
    await client.create_creative(
        name="c",
        image_hash="abc",
        message="Hello",
        headline="Head",
        link="https://example.com",
        call_to_action_type="LEARN_MORE",
    )
    assert "page_id" in seen["body"]
    assert "page-999" in seen["body"]
    assert "image_hash" in seen["body"]


@pytest.mark.asyncio
async def test_create_creative_video_branch() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"id": "cr2"})

    client = _client(handler, page_id="page-1")
    await client.create_creative(
        name="c", video_id="v-1", message="watch this"
    )
    assert "video_data" in seen["body"]
    assert "v-1" in seen["body"]


# ── uploads ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_image_returns_hash(tmp_path: Path) -> None:
    p = tmp_path / "asset.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\npayload")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        assert "/adimages" in str(req.url)
        return httpx.Response(
            200,
            json={
                "images": {
                    "asset.png": {
                        "hash": "HASH123",
                        "url": "https://cdn.meta/asset.png",
                    }
                }
            },
        )

    client = _client(handler)
    out = await client.upload_image(p)
    assert out["hash"] == "HASH123"


@pytest.mark.asyncio
async def test_upload_image_missing_file_raises(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(MetaAdsError):
        await client.upload_image(tmp_path / "nope.png")


@pytest.mark.asyncio
async def test_upload_video_returns_id(tmp_path: Path) -> None:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00\x00\x00\x20ftypmp42" + b"x" * 32)

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/advideos" in str(req.url)
        return httpx.Response(200, json={"id": "VID-99"})

    client = _client(handler)
    out = await client.upload_video(p, title="test")
    assert out["id"] == "VID-99"


# ── status / budget ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_status_normalises_case() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"success": True})

    client = _client(handler)
    out = await client.set_status("c1", "active")
    assert out["success"] is True
    assert "status=ACTIVE" in seen["body"]


@pytest.mark.asyncio
async def test_set_status_rejects_bogus_value() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(MetaAdsError) as ei:
        await client.set_status("c1", "WHATEVER")
    assert "ACTIVE" in ei.value.message


@pytest.mark.asyncio
async def test_update_budget_requires_value() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(handler)
    with pytest.raises(MetaAdsError):
        await client.update_budget("c1")


@pytest.mark.asyncio
async def test_update_budget_happy_path() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"success": True})

    client = _client(handler)
    await client.update_budget("c1", daily_budget=3000)
    assert "daily_budget=3000" in seen["body"]


# ── upstream error surfacing ────────────────────────────────────


@pytest.mark.asyncio
async def test_upstream_4xx_raises_meta_ads_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid OAuth access token.",
                    "code": 190,
                }
            },
        )

    client = _client(handler)
    with pytest.raises(MetaAdsError) as ei:
        await client.list_campaigns()
    assert ei.value.status == 400
    assert "Invalid OAuth" in ei.value.message


@pytest.mark.asyncio
async def test_upstream_non_json_body_still_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    client = _client(handler)
    with pytest.raises(MetaAdsError) as ei:
        await client.list_campaigns()
    assert ei.value.status == 500
