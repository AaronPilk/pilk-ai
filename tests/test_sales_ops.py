"""Unit tests for the sales_ops toolkit.

Network calls are stubbed via ``httpx.MockTransport`` so the tests run
entirely offline. Each test exercises one of:

- "not configured" error paths (no env var set)
- input validation (missing required arg)
- happy-path parsing of upstream JSON
- upstream error surfacing (HTTP 4xx/5xx from the API)
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from core.config import get_settings
from core.tools.builtin.sales_ops import (
    google_places_search_tool,
    hunter_find_email_tool,
    site_audit_tool,
)
from core.tools.registry import ToolContext


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Redirect every `httpx.AsyncClient()` in the sales_ops module at
    an in-process MockTransport. Keeps the tests hermetic without
    touching httpx's global state."""

    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


# ── google_places_search ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_google_places_requires_query() -> None:
    out = await google_places_search_tool.handler(
        {"query": ""}, ToolContext()
    )
    assert out.is_error
    assert "query" in out.content.lower()


@pytest.mark.asyncio
async def test_google_places_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.delenv("PILK_GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("PILK_GOOGLE_API_KEY", raising=False)
    out = await google_places_search_tool.handler(
        {"query": "cpas in tampa"}, ToolContext()
    )
    assert out.is_error
    assert "google places" in out.content.lower()


@pytest.mark.asyncio
async def test_google_places_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")

    def handler(req: httpx.Request) -> httpx.Response:
        assert "places.googleapis.com" in str(req.url)
        return httpx.Response(
            200,
            json={
                "places": [
                    {
                        "id": "abc123",
                        "displayName": {"text": "Tampa CPA LLC"},
                        "formattedAddress": "1 Main St, Tampa",
                        "websiteUri": "https://tampacpa.example",
                        "nationalPhoneNumber": "(813) 555-0100",
                        "rating": 4.5,
                        "userRatingCount": 42,
                    }
                ]
            },
        )

    _install_transport(monkeypatch, handler)
    out = await google_places_search_tool.handler(
        {"query": "cpas in tampa", "limit": 5}, ToolContext()
    )
    assert not out.is_error
    assert out.data["results"][0]["name"] == "Tampa CPA LLC"
    assert out.data["results"][0]["website"] == "https://tampacpa.example"


# ── site_audit ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_site_audit_requires_url() -> None:
    out = await site_audit_tool.handler({"url": ""}, ToolContext())
    assert out.is_error
    assert "url" in out.content.lower()


@pytest.mark.asyncio
async def test_site_audit_bad_score_scaled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("PAGESPEED_API_KEY", "test-key")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "lighthouseResult": {
                    "categories": {"performance": {"score": 0.35}},
                    "audits": {
                        "largest-contentful-paint": {"displayValue": "4.2 s"},
                        "cumulative-layout-shift": {"displayValue": "0.12"},
                        "total-blocking-time": {"displayValue": "520 ms"},
                        "first-contentful-paint": {"displayValue": "2.1 s"},
                    },
                }
            },
        )

    _install_transport(monkeypatch, handler)
    # Explicit http:// so the SSL-penalty branch fires.
    out = await site_audit_tool.handler(
        {"url": "http://tampacpa.example"}, ToolContext()
    )
    assert not out.is_error
    # performance 0.35 → bad = 65; http-only → +10 → 75
    assert out.data["bad_site_score"] == 75
    assert out.data["ssl"] is False
    assert out.data["lcp"] == "4.2 s"

    # Re-run with https:// and confirm the penalty drops off.
    out_https = await site_audit_tool.handler(
        {"url": "https://tampacpa.example"}, ToolContext()
    )
    assert out_https.data["bad_site_score"] == 65
    assert out_https.data["ssl"] is True


# ── hunter_find_email ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hunter_domain_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("HUNTER_IO_API_KEY", "test-key")

    def handler(req: httpx.Request) -> httpx.Response:
        assert "api.hunter.io" in str(req.url)
        assert "domain-search" in str(req.url)
        return httpx.Response(
            200,
            json={
                "data": {
                    "emails": [
                        {
                            "value": "owner@acme.com",
                            "first_name": "Jane",
                            "last_name": "Doe",
                            "position": "Owner",
                        }
                    ]
                }
            },
        )

    _install_transport(monkeypatch, handler)
    out = await hunter_find_email_tool.handler(
        {"domain": "acme.com"}, ToolContext()
    )
    assert not out.is_error
    assert out.data["emails"][0]["value"] == "owner@acme.com"


@pytest.mark.asyncio
async def test_hunter_email_finder_picks_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("HUNTER_IO_API_KEY", "test-key")

    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(
            200,
            json={
                "data": {
                    "email": "jane.doe@acme.com",
                    "score": 95,
                }
            },
        )

    _install_transport(monkeypatch, handler)
    out = await hunter_find_email_tool.handler(
        {"domain": "acme.com", "first_name": "Jane", "last_name": "Doe"},
        ToolContext(),
    )
    assert not out.is_error
    assert "email-finder" in seen["url"]
    assert out.data["email"] == "jane.doe@acme.com"


