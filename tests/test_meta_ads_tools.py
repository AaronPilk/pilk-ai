"""Tool-level tests for meta_ads builtin tools.

Covers the risk-class assignment, input validation, "not configured"
paths, workspace-scoped upload path resolution + escape guards, and
happy-path ToolOutcome shape on reads / creates / status / insights.

Network is stubbed via ``httpx.MockTransport`` so every test runs
offline.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from core.config import get_settings
from core.policy.risk import RiskClass
from core.tools.builtin.meta_ads import (
    META_ADS_TOOLS,
    meta_ads_create_ad_tool,
    meta_ads_create_adset_tool,
    meta_ads_create_campaign_tool,
    meta_ads_create_creative_tool,
    meta_ads_get_insights_tool,
    meta_ads_list_campaigns_tool,
    meta_ads_set_status_tool,
    meta_ads_update_budget_tool,
    meta_ads_upload_image_tool,
    meta_ads_upload_video_tool,
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
    """Populate the env so _client() resolves."""
    get_settings.cache_clear()
    monkeypatch.setenv("META_ACCESS_TOKEN", "tok-abc")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "12345")
    monkeypatch.setenv("META_PAGE_ID", "page-1")


def _clear_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    for k in (
        "META_ACCESS_TOKEN", "PILK_META_ACCESS_TOKEN",
        "FB_ACCESS_TOKEN", "FACEBOOK_ACCESS_TOKEN",
        "META_AD_ACCOUNT_ID", "PILK_META_AD_ACCOUNT_ID",
        "FB_AD_ACCOUNT_ID",
        "META_PAGE_ID", "PILK_META_PAGE_ID", "FB_PAGE_ID",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(sandbox_root=tmp_path)


# ── tool registry / risk classes ────────────────────────────────


def test_tool_count_is_twelve() -> None:
    assert len(META_ADS_TOOLS) == 12


def test_tool_names_unique() -> None:
    names = [t.name for t in META_ADS_TOOLS]
    assert len(names) == len(set(names))
    for n in names:
        assert n.startswith("meta_ads_")


def test_reads_are_net_read() -> None:
    read_names = {
        "meta_ads_list_campaigns",
        "meta_ads_list_adsets",
        "meta_ads_list_ads",
        "meta_ads_get_insights",
    }
    for t in META_ADS_TOOLS:
        if t.name in read_names:
            assert t.risk == RiskClass.NET_READ, t.name


def test_creates_and_uploads_are_net_write() -> None:
    write_names = {
        "meta_ads_create_campaign",
        "meta_ads_create_adset",
        "meta_ads_create_ad",
        "meta_ads_create_creative",
        "meta_ads_upload_image",
        "meta_ads_upload_video",
    }
    for t in META_ADS_TOOLS:
        if t.name in write_names:
            assert t.risk == RiskClass.NET_WRITE, t.name


def test_status_and_budget_are_financial() -> None:
    """set_status and update_budget are the two knobs that actually
    spend money — they must trip the FINANCIAL approval gate."""
    for t in META_ADS_TOOLS:
        if t.name in {"meta_ads_set_status", "meta_ads_update_budget"}:
            assert t.risk == RiskClass.FINANCIAL, t.name


# ── not-configured error path ───────────────────────────────────


@pytest.mark.asyncio
async def test_missing_credentials_surfaces_clean_error(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _clear_creds(monkeypatch)
    out = await meta_ads_list_campaigns_tool.handler({}, ctx)
    assert out.is_error
    assert "meta ads" in out.content.lower()
    assert "settings" in out.content.lower()


# ── input validation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_campaign_requires_name_and_objective(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await meta_ads_create_campaign_tool.handler({}, ctx)
    assert out.is_error
    assert "name" in out.content.lower() or "objective" in out.content.lower()


@pytest.mark.asyncio
async def test_create_adset_requires_fields(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await meta_ads_create_adset_tool.handler(
        {"name": "x"}, ctx
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_create_ad_requires_all_ids(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await meta_ads_create_ad_tool.handler(
        {"name": "x"}, ctx
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_create_creative_requires_hash_or_video(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await meta_ads_create_creative_tool.handler(
        {"name": "test"}, ctx
    )
    assert out.is_error
    assert "image_hash" in out.content or "video_id" in out.content


@pytest.mark.asyncio
async def test_get_insights_requires_object_id(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await meta_ads_get_insights_tool.handler({}, ctx)
    assert out.is_error
    assert "object_id" in out.content


@pytest.mark.asyncio
async def test_set_status_requires_both_fields(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await meta_ads_set_status_tool.handler(
        {"object_id": "c1"}, ctx
    )
    assert out.is_error


@pytest.mark.asyncio
async def test_update_budget_requires_a_value(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await meta_ads_update_budget_tool.handler(
        {"object_id": "c1"}, ctx
    )
    assert out.is_error


# ── happy paths ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_campaigns_formats_summary(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "c1",
                        "name": "Tampa Traffic Test",
                        "objective": "OUTCOME_TRAFFIC",
                        "effective_status": "PAUSED",
                    }
                ]
            },
        ),
    )
    out = await meta_ads_list_campaigns_tool.handler({}, ctx)
    assert not out.is_error, out.content
    assert "Tampa Traffic Test" in out.content
    assert out.data["campaigns"][0]["id"] == "c1"


@pytest.mark.asyncio
async def test_create_campaign_reports_paused(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"id": "c-new"}),
    )
    out = await meta_ads_create_campaign_tool.handler(
        {"name": "T", "objective": "OUTCOME_TRAFFIC"}, ctx
    )
    assert not out.is_error, out.content
    assert "PAUSED" in out.content
    assert out.data["id"] == "c-new"


@pytest.mark.asyncio
async def test_get_insights_summary_formats_numeric_fields(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "impressions": "1000",
                        "clicks": "25",
                        "spend": "12.34",
                        "ctr": "2.5",
                    }
                ]
            },
        ),
    )
    out = await meta_ads_get_insights_tool.handler(
        {"object_id": "c1"}, ctx
    )
    assert not out.is_error, out.content
    assert "impressions" in out.content
    assert "1000" in out.content


@pytest.mark.asyncio
async def test_get_insights_empty_is_not_error(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"data": []}),
    )
    out = await meta_ads_get_insights_tool.handler(
        {"object_id": "c1"}, ctx
    )
    assert not out.is_error
    assert "no insights" in out.content.lower()


@pytest.mark.asyncio
async def test_set_status_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"success": True}),
    )
    out = await meta_ads_set_status_tool.handler(
        {"object_id": "c1", "status": "PAUSED"}, ctx
    )
    assert not out.is_error, out.content
    assert out.data["object_id"] == "c1"
    assert out.data["status"] == "PAUSED"


# ── upload path resolution ──────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_image_rejects_escape_path(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await meta_ads_upload_image_tool.handler(
        {"path": "../../etc/passwd"}, ctx
    )
    assert out.is_error
    assert "workspace" in out.content.lower() or "escape" in out.content.lower()


@pytest.mark.asyncio
async def test_upload_image_rejects_missing_file(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await meta_ads_upload_image_tool.handler(
        {"path": "creative/does_not_exist.png"}, ctx
    )
    assert out.is_error
    assert "not found" in out.content.lower()


@pytest.mark.asyncio
async def test_upload_image_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
    tmp_path: Path,
) -> None:
    _set_creds(monkeypatch)
    (tmp_path / "creative").mkdir()
    img = tmp_path / "creative" / "hero.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\npayload")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "images": {
                    "hero.png": {
                        "hash": "H-1",
                        "url": "https://cdn.meta/hero.png",
                    }
                }
            },
        )

    _install_transport(monkeypatch, handler)
    out = await meta_ads_upload_image_tool.handler(
        {"path": "creative/hero.png"}, ctx
    )
    assert not out.is_error, out.content
    assert out.data["hash"] == "H-1"


@pytest.mark.asyncio
async def test_upload_video_rejects_missing_path(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
) -> None:
    _set_creds(monkeypatch)
    out = await meta_ads_upload_video_tool.handler({}, ctx)
    assert out.is_error
    assert "path" in out.content.lower()


@pytest.mark.asyncio
async def test_upload_video_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
    tmp_path: Path,
) -> None:
    _set_creds(monkeypatch)
    (tmp_path / "creative").mkdir()
    vid = tmp_path / "creative" / "clip.mp4"
    vid.write_bytes(b"\x00\x00\x00\x20ftypmp42" + b"x" * 16)

    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"id": "V-1"}),
    )
    out = await meta_ads_upload_video_tool.handler(
        {"path": "creative/clip.mp4", "title": "Test"}, ctx
    )
    assert not out.is_error, out.content
    assert out.data["id"] == "V-1"
