"""Tool-level tests for google_ads builtin tools.

Covers registry shape, risk class assignments, "not configured"
paths, input validation on each tool, and happy-path ToolOutcome
shapes with network stubbed via httpx.MockTransport.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from core.config import get_settings
from core.policy.risk import RiskClass
from core.tools.builtin.google_ads import (
    GOOGLE_ADS_TOOLS,
    google_ads_add_keywords_tool,
    google_ads_add_negative_keywords_tool,
    google_ads_create_ad_group_tool,
    google_ads_create_budget_tool,
    google_ads_create_campaign_tool,
    google_ads_get_metrics_tool,
    google_ads_list_campaigns_tool,
    google_ads_run_gaql_tool,
    google_ads_set_status_tool,
    google_ads_update_budget_tool,
)
from core.tools.registry import ToolContext


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _set_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "dev-abc")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GOOGLE_ADS_REFRESH_TOKEN", "rtok")
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", "1234567890")


def _clear_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    for k in (
        "GOOGLE_ADS_DEVELOPER_TOKEN", "PILK_GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID", "PILK_GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET", "PILK_GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN", "PILK_GOOGLE_ADS_REFRESH_TOKEN",
        "GOOGLE_ADS_CUSTOMER_ID", "PILK_GOOGLE_ADS_CUSTOMER_ID",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID", "PILK_GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    ):
        monkeypatch.delenv(k, raising=False)


def _ok_oauth(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": "access-fresh", "expires_in": 3600},
    )


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(sandbox_root=None)


# ── registry shape ──────────────────────────────────────────────


def test_tool_count_is_13() -> None:
    assert len(GOOGLE_ADS_TOOLS) == 13


def test_tool_names_unique_and_prefixed() -> None:
    names = [t.name for t in GOOGLE_ADS_TOOLS]
    assert len(names) == len(set(names))
    for n in names:
        assert n.startswith("google_ads_")


def test_reads_are_net_read() -> None:
    read_names = {
        "google_ads_list_campaigns",
        "google_ads_list_ad_groups",
        "google_ads_list_ads",
        "google_ads_get_metrics",
        "google_ads_run_gaql",
    }
    for t in GOOGLE_ADS_TOOLS:
        if t.name in read_names:
            assert t.risk == RiskClass.NET_READ, t.name


def test_creates_are_net_write() -> None:
    write_names = {
        "google_ads_create_budget",
        "google_ads_create_campaign",
        "google_ads_create_ad_group",
        "google_ads_add_keywords",
        "google_ads_add_negative_keywords",
        "google_ads_create_responsive_search_ad",
    }
    for t in GOOGLE_ADS_TOOLS:
        if t.name in write_names:
            assert t.risk == RiskClass.NET_WRITE, t.name


def test_status_and_budget_are_financial() -> None:
    """Activation spends money. Budget change reshapes spend. Both
    must trip the FINANCIAL gate so the operator consents before the
    change lands."""
    for t in GOOGLE_ADS_TOOLS:
        if t.name in {"google_ads_set_status", "google_ads_update_budget"}:
            assert t.risk == RiskClass.FINANCIAL, t.name


# ── not configured ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_not_configured_surfaces_cleanly(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _clear_creds(monkeypatch)
    out = await google_ads_list_campaigns_tool.handler({}, ctx)
    assert out.is_error
    assert "Google Ads not configured" in out.content
    # Name every missing secret so the operator knows what to paste.
    for k in (
        "google_ads_developer_token",
        "google_ads_client_id",
        "google_ads_client_secret",
        "google_ads_refresh_token",
        "google_ads_customer_id",
    ):
        assert k in out.content


# ── arg validation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_budget_validates_amount(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)
    out = await google_ads_create_budget_tool.handler(
        {"name": "B1", "daily_usd": -5}, ctx,
    )
    assert out.is_error
    assert "positive" in out.content


@pytest.mark.asyncio
async def test_create_campaign_requires_budget_resource(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)
    out = await google_ads_create_campaign_tool.handler(
        {"name": "C1"}, ctx,
    )
    assert out.is_error
    assert "budget_resource" in out.content


@pytest.mark.asyncio
async def test_create_ad_group_requires_campaign_resource(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)
    out = await google_ads_create_ad_group_tool.handler(
        {"name": "G1"}, ctx,
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_add_keywords_requires_non_empty_list(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)
    out = await google_ads_add_keywords_tool.handler(
        {"ad_group_resource": "customers/1/adGroups/1", "keywords": []}, ctx,
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_add_negative_keywords_requires_list(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)
    out = await google_ads_add_negative_keywords_tool.handler(
        {"campaign_resource": "customers/1/campaigns/1"}, ctx,
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_run_gaql_rejects_non_select(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)
    out = await google_ads_run_gaql_tool.handler(
        {"query": "DELETE FROM campaign"}, ctx,
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_set_status_rejects_invalid(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)
    out = await google_ads_set_status_tool.handler(
        {
            "campaign_resource": "customers/1/campaigns/1",
            "status": "ARCHIVED",
        },
        ctx,
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_update_budget_rejects_zero(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)
    out = await google_ads_update_budget_tool.handler(
        {"budget_resource": "customers/1/campaignBudgets/1", "daily_usd": 0},
        ctx,
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_get_metrics_rejects_invalid_level(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)
    out = await google_ads_get_metrics_tool.handler(
        {"level": "KEYWORD"}, ctx,
    )
    assert out.is_error


# ── happy paths ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_campaigns_happy_path(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth(req)
        return httpx.Response(
            200,
            json=[
                {
                    "results": [
                        {
                            "campaign": {
                                "id": "111",
                                "name": "Tampa CPA",
                                "status": "PAUSED",
                                "advertisingChannelType": "SEARCH",
                                "biddingStrategyType": "MANUAL_CPC",
                            },
                            "campaignBudget": {
                                "amountMicros": "50000000",
                            },
                        }
                    ]
                }
            ],
        )

    _install_transport(monkeypatch, handler)
    out = await google_ads_list_campaigns_tool.handler({}, ctx)
    assert not out.is_error
    c = out.data["campaigns"][0]
    assert c["id"] == "111"
    assert c["name"] == "Tampa CPA"
    assert c["daily_budget_usd"] == "50.00"


@pytest.mark.asyncio
async def test_create_budget_happy_path(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth(req)
        import json as _json
        captured.append(_json.loads(req.content.decode()))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "resourceName": (
                            "customers/1234567890/campaignBudgets/77"
                        )
                    }
                ]
            },
        )

    _install_transport(monkeypatch, handler)
    out = await google_ads_create_budget_tool.handler(
        {"name": "Tampa daily", "daily_usd": 50}, ctx,
    )
    assert not out.is_error
    assert "77" in out.data["resource_name"]
    # Verify micros translation.
    op = captured[0]["operations"][0]["create"]
    assert op["amountMicros"] == "50000000"


@pytest.mark.asyncio
async def test_add_keywords_happy_path(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth(req)
        return httpx.Response(200, json={"results": [{}, {}]})

    _install_transport(monkeypatch, handler)
    out = await google_ads_add_keywords_tool.handler(
        {
            "ad_group_resource": "customers/1234567890/adGroups/5",
            "keywords": [
                {"text": "tax prep tampa", "match_type": "PHRASE"},
                {"text": "cpa near me", "match_type": "EXACT"},
            ],
        },
        ctx,
    )
    assert not out.is_error
    assert "Added 2 keyword" in out.content


@pytest.mark.asyncio
async def test_set_status_happy_path(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_creds(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return _ok_oauth(req)
        return httpx.Response(200, json={"results": [{}]})

    _install_transport(monkeypatch, handler)
    out = await google_ads_set_status_tool.handler(
        {
            "campaign_resource": "customers/1234567890/campaigns/99",
            "status": "PAUSED",
        },
        ctx,
    )
    assert not out.is_error
    assert out.data["status"] == "PAUSED"
