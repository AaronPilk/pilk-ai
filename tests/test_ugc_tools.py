"""Tool-level tests for ugc builtin tools.

Covers:
- Tool registry shape (count, name prefix, uniqueness)
- Risk class assignments (reads = NET_READ, export = WRITE_LOCAL)
- "Not configured" surfacing when Apify / Hunter keys are missing
- Hashtag / username argument validation
- Happy-path shape for IG hashtag search (normalised + deduplicated)
- Email finder: bio regex hit → no external call
- Email finder: missing bio + missing domain → error
- CSV export: writes correct columns + escapes to workspace
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from core.config import get_settings
from core.policy.risk import RiskClass
from core.tools.builtin.ugc import (
    UGC_CSV_COLUMNS,
    UGC_TOOLS,
    ugc_export_csv_tool,
    ugc_find_email_tool,
    ugc_instagram_hashtag_search_tool,
    ugc_instagram_profile_tool,
    ugc_tiktok_hashtag_search_tool,
    ugc_tiktok_profile_tool,
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


def _set_apify(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("APIFY_API_TOKEN", "tok-apify")


def _set_hunter(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("HUNTER_IO_API_KEY", "tok-hunter")


def _clear_apify(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    for k in ("APIFY_API_TOKEN", "PILK_APIFY_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def _clear_hunter(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    for k in ("HUNTER_IO_API_KEY", "PILK_HUNTER_IO_API_KEY"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(sandbox_root=tmp_path)


# ── tool registry shape ─────────────────────────────────────────


def test_tool_count_is_six() -> None:
    assert len(UGC_TOOLS) == 6


def test_tool_names_unique_and_prefixed() -> None:
    names = [t.name for t in UGC_TOOLS]
    assert len(names) == len(set(names))
    for n in names:
        assert n.startswith("ugc_")


def test_search_and_profile_tools_are_net_read() -> None:
    net_read = {
        "ugc_instagram_hashtag_search",
        "ugc_instagram_profile",
        "ugc_tiktok_hashtag_search",
        "ugc_tiktok_profile",
        "ugc_find_email",
    }
    for t in UGC_TOOLS:
        if t.name in net_read:
            assert t.risk == RiskClass.NET_READ, t.name


def test_export_csv_is_write_local() -> None:
    assert ugc_export_csv_tool.risk == RiskClass.WRITE_LOCAL


# ── not configured ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ig_search_without_key_is_clean_error(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _clear_apify(monkeypatch)
    out = await ugc_instagram_hashtag_search_tool.handler(
        {"hashtag": "skincare"}, ctx
    )
    assert out.is_error
    assert "Apify not configured" in out.content


@pytest.mark.asyncio
async def test_tt_search_without_key_is_clean_error(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _clear_apify(monkeypatch)
    out = await ugc_tiktok_hashtag_search_tool.handler(
        {"hashtag": "dance"}, ctx
    )
    assert out.is_error
    assert "Apify" in out.content


# ── arg validation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ig_hashtag_search_requires_hashtag(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_apify(monkeypatch)
    out = await ugc_instagram_hashtag_search_tool.handler({}, ctx)
    assert out.is_error
    assert "hashtag" in out.content.lower()


@pytest.mark.asyncio
async def test_ig_profile_requires_username(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_apify(monkeypatch)
    out = await ugc_instagram_profile_tool.handler({}, ctx)
    assert out.is_error


@pytest.mark.asyncio
async def test_tt_profile_requires_username(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_apify(monkeypatch)
    out = await ugc_tiktok_profile_tool.handler({}, ctx)
    assert out.is_error


# ── IG hashtag search happy path (normalisation + dedupe) ────────


@pytest.mark.asyncio
async def test_ig_hashtag_search_dedupes_by_handle(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_apify(monkeypatch)
    _install_transport(
        monkeypatch,
        lambda _r: httpx.Response(
            200,
            json=[
                {
                    "ownerUsername": "anna",
                    "caption": "glow routine 🧴",
                    "url": "https://instagram.com/p/a1",
                    "likesCount": 1200,
                    "commentsCount": 40,
                    "type": "Image",
                },
                {
                    "ownerUsername": "anna",
                    "caption": "morning routine",
                    "url": "https://instagram.com/p/a2",
                    "likesCount": 900,
                    "type": "Video",
                },
                {
                    "ownerUsername": "sam",
                    "caption": "serum review",
                    "url": "https://instagram.com/p/s1",
                    "likesCount": 500,
                    "type": "Image",
                },
            ],
        ),
    )
    out = await ugc_instagram_hashtag_search_tool.handler(
        {"hashtag": "#skincare", "limit": 10}, ctx
    )
    assert not out.is_error
    assert out.data["creators"][0]["handle"] in {"anna", "sam"}
    # Anna's 2 posts → 1 creator row; Sam → 1 creator row.
    assert len(out.data["creators"]) == 2
    anna = next(c for c in out.data["creators"] if c["handle"] == "anna")
    # Second post URL landed in other_posts.
    assert "https://instagram.com/p/a2" in anna["other_posts"]


# ── email finder ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_email_bio_hit_skips_hunter(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    """Bio email found → should NOT call Hunter.io."""
    _clear_hunter(monkeypatch)  # hunter intentionally unset
    out = await ugc_find_email_tool.handler(
        {
            "bio": "skincare fanatic 🌸 collabs: Jane@glowbrand.co",
        },
        ctx,
    )
    assert not out.is_error
    assert out.data["email"].lower() == "jane@glowbrand.co"
    assert out.data["source"] == "bio"


@pytest.mark.asyncio
async def test_find_email_no_bio_no_domain_errors(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _clear_hunter(monkeypatch)
    out = await ugc_find_email_tool.handler({}, ctx)
    assert out.is_error


@pytest.mark.asyncio
async def test_find_email_hunter_fallback(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    _set_hunter(monkeypatch)
    _install_transport(
        monkeypatch,
        lambda _r: httpx.Response(
            200,
            json={
                "data": {
                    "email": "jane@glowbrand.co",
                    "score": 87,
                    "verification": {"status": "valid"},
                }
            },
        ),
    )
    out = await ugc_find_email_tool.handler(
        {"domain": "glowbrand.co", "full_name": "Jane Doe"},
        ctx,
    )
    assert not out.is_error
    assert out.data["email"] == "jane@glowbrand.co"
    assert out.data["source"] == "hunter"
    assert 0.8 <= out.data["confidence"] <= 0.9


# ── CSV export ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_csv_writes_standard_columns(
    ctx: ToolContext, tmp_path: Path
) -> None:
    out = await ugc_export_csv_tool.handler(
        {
            "path": "ugc/test",  # trailing `.csv` added automatically
            "creators": [
                {
                    "handle": "anna",
                    "platform": "instagram",
                    "followers": 24000,
                    "score_overall": 7.8,
                    "score_quality": 8,
                    "score_brand_fit": 8,
                    "score_business_utility": 8,
                    "score_virality": 6,
                    "score_cringe_risk": 2,
                    "email": "anna@x.co",
                    "email_source": "bio",
                    "email_confidence": 0.95,
                    "profile_url": "https://instagram.com/anna",
                    "top_post_url": "https://instagram.com/p/a1",
                    "notes": "Tight hook, real demo.",
                },
            ],
        },
        ctx,
    )
    assert not out.is_error
    assert out.data["rows"] == 1
    csv_path = tmp_path / "ugc" / "test.csv"
    assert csv_path.is_file()
    with csv_path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == list(UGC_CSV_COLUMNS)
    assert rows[0]["handle"] == "anna"
    assert rows[0]["email"] == "anna@x.co"


@pytest.mark.asyncio
async def test_export_csv_rejects_workspace_escape(
    ctx: ToolContext, tmp_path: Path
) -> None:
    out = await ugc_export_csv_tool.handler(
        {
            "path": "../../../etc/passwd.csv",
            "creators": [{"handle": "x"}],
        },
        ctx,
    )
    assert out.is_error
    assert "escapes workspace" in out.content


@pytest.mark.asyncio
async def test_export_csv_requires_non_empty_creators(
    ctx: ToolContext,
) -> None:
    out = await ugc_export_csv_tool.handler(
        {"path": "ugc/empty.csv", "creators": []},
        ctx,
    )
    assert out.is_error
