"""Arcads client + three tools (list_products / generate / status).

External API mocked through httpx.MockTransport — we never hit
external-api.arcads.ai from the suite. The integration-secrets store
is seeded per-test so the missing-key branch and the happy path both
run cleanly in the same file.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from core.config import get_settings
from core.db import ensure_schema
from core.integrations.arcads import ArcadsClient, ArcadsError
from core.secrets import (
    IntegrationSecretsStore,
    set_integration_secrets_store,
)
from core.tools.builtin.arcads import (
    arcads_list_products_tool,
    arcads_video_generate_tool,
    arcads_video_status_tool,
)
from core.tools.registry import ToolContext

# ── helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def secrets_store() -> IntegrationSecretsStore:
    settings = get_settings()
    ensure_schema(settings.db_path)
    store = IntegrationSecretsStore(settings.db_path)
    set_integration_secrets_store(store)
    # Ensure no stale key leaks across tests.
    store.delete("arcads_api_key")
    yield store
    store.delete("arcads_api_key")
    set_integration_secrets_store(None)


def _mock_httpx(monkeypatch, handler) -> None:
    """Route the module's httpx.AsyncClient through MockTransport so we
    never hit the real Arcads host."""
    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def patched(*args, **kwargs):
        # Drop any caller-supplied kwargs that MockTransport doesn't
        # care about (timeout, auth, etc. still work via the transport).
        return orig(transport=transport, timeout=kwargs.get("timeout", 10))

    monkeypatch.setattr(
        "core.integrations.arcads.httpx.AsyncClient",
        patched,
    )


# ── arcads_list_products ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_products_missing_key_returns_friendly_error(
    secrets_store: IntegrationSecretsStore,
) -> None:
    out = await arcads_list_products_tool.handler({}, ToolContext())
    assert out.is_error
    assert "arcads_api_key" in out.content.lower()
    assert "settings" in out.content.lower()


@pytest.mark.asyncio
async def test_list_products_happy_path(
    secrets_store: IntegrationSecretsStore, monkeypatch
) -> None:
    secrets_store.upsert("arcads_api_key", "test-key")
    seen_auth: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers.get("authorization"))
        assert req.url.path == "/v1/products"
        return httpx.Response(
            200,
            json=[
                {"id": "p1", "name": "Kitchen Offer", "description": "..."},
                {"id": "p2", "name": "Cold Brew", "description": "..."},
            ],
        )

    _mock_httpx(monkeypatch, handler)
    out = await arcads_list_products_tool.handler({}, ToolContext())
    assert not out.is_error
    assert out.data["count"] == 2
    ids = [p["id"] for p in out.data["products"]]
    assert ids == ["p1", "p2"]
    # HTTP Basic was attached (key as username, empty password).
    assert seen_auth and seen_auth[0] and seen_auth[0].startswith("Basic ")


@pytest.mark.asyncio
async def test_list_products_accepts_paginated_response(
    secrets_store: IntegrationSecretsStore, monkeypatch
) -> None:
    secrets_store.upsert("arcads_api_key", "test-key")
    _mock_httpx(
        monkeypatch,
        lambda _r: httpx.Response(
            200, json={"items": [{"id": "p1", "name": "Only"}]}
        ),
    )
    out = await arcads_list_products_tool.handler({}, ToolContext())
    assert out.data["count"] == 1
    assert out.data["products"][0]["id"] == "p1"


@pytest.mark.asyncio
async def test_list_products_empty_returns_friendly_message(
    secrets_store: IntegrationSecretsStore, monkeypatch
) -> None:
    secrets_store.upsert("arcads_api_key", "test-key")
    _mock_httpx(monkeypatch, lambda _r: httpx.Response(200, json=[]))
    out = await arcads_list_products_tool.handler({}, ToolContext())
    assert not out.is_error
    assert "No products found" in out.content


@pytest.mark.asyncio
async def test_list_products_surfaces_http_error(
    secrets_store: IntegrationSecretsStore, monkeypatch
) -> None:
    secrets_store.upsert("arcads_api_key", "bad-key")
    _mock_httpx(
        monkeypatch,
        lambda _r: httpx.Response(401, text="Unauthorized"),
    )
    out = await arcads_list_products_tool.handler({}, ToolContext())
    assert out.is_error
    assert "401" in out.content


# ── arcads_video_generate ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_video_generate_requires_product_id(
    secrets_store: IntegrationSecretsStore,
) -> None:
    secrets_store.upsert("arcads_api_key", "test-key")
    out = await arcads_video_generate_tool.handler(
        {"prompt": "hi"}, ToolContext()
    )
    assert out.is_error
    assert "product_id" in out.content


@pytest.mark.asyncio
async def test_video_generate_requires_prompt(
    secrets_store: IntegrationSecretsStore,
) -> None:
    secrets_store.upsert("arcads_api_key", "test-key")
    out = await arcads_video_generate_tool.handler(
        {"product_id": "p1"}, ToolContext()
    )
    assert out.is_error
    assert "prompt" in out.content


@pytest.mark.asyncio
async def test_video_generate_posts_expected_body(
    secrets_store: IntegrationSecretsStore, monkeypatch
) -> None:
    secrets_store.upsert("arcads_api_key", "test-key")
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v2/videos/generate"
        assert req.method == "POST"
        import json as _json

        captured["body"] = _json.loads(req.content.decode("utf-8"))
        return httpx.Response(
            201,
            json={
                "id": "asset_123",
                "status": "pending",
                "data": {"creditsCharged": 720},
            },
        )

    _mock_httpx(monkeypatch, handler)
    out = await arcads_video_generate_tool.handler(
        {
            "product_id": "p1",
            "prompt": "15s UGC skincare review",
            "duration_s": 15,
            "audio_enabled": True,
        },
        ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["asset_id"] == "asset_123"
    assert out.data["status"] == "pending"
    assert out.data["credits_charged"] == 720
    # Default model is seedance-2.0, aspect ratio 9:16.
    assert captured["body"]["model"] == "seedance-2.0"
    assert captured["body"]["aspectRatio"] == "9:16"
    assert captured["body"]["duration"] == 15
    assert captured["body"]["audioEnabled"] is True
    assert captured["body"]["productId"] == "p1"
    assert captured["body"]["prompt"] == "15s UGC skincare review"


@pytest.mark.asyncio
async def test_video_generate_surfaces_500_from_arcads(
    secrets_store: IntegrationSecretsStore, monkeypatch
) -> None:
    secrets_store.upsert("arcads_api_key", "test-key")
    _mock_httpx(
        monkeypatch,
        lambda _r: httpx.Response(500, text="UNKNOWN_ERROR"),
    )
    out = await arcads_video_generate_tool.handler(
        {"product_id": "p1", "prompt": "hi"}, ToolContext()
    )
    assert out.is_error
    assert "500" in out.content


# ── arcads_video_status ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_video_status_requires_asset_id(
    secrets_store: IntegrationSecretsStore,
) -> None:
    secrets_store.upsert("arcads_api_key", "test-key")
    out = await arcads_video_status_tool.handler({}, ToolContext())
    assert out.is_error


@pytest.mark.asyncio
async def test_video_status_returns_url_when_generated(
    secrets_store: IntegrationSecretsStore, monkeypatch
) -> None:
    secrets_store.upsert("arcads_api_key", "test-key")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/assets/asset_123"
        return httpx.Response(
            200,
            json={
                "id": "asset_123",
                "status": "generated",
                "url": "https://cdn.arcads.ai/render/abc.mp4",
            },
        )

    _mock_httpx(monkeypatch, handler)
    out = await arcads_video_status_tool.handler(
        {"asset_id": "asset_123"}, ToolContext()
    )
    assert not out.is_error
    assert out.data["status"] == "generated"
    assert out.data["video_url"] == "https://cdn.arcads.ai/render/abc.mp4"


@pytest.mark.asyncio
async def test_video_status_surfaces_failure_reason(
    secrets_store: IntegrationSecretsStore, monkeypatch
) -> None:
    secrets_store.upsert("arcads_api_key", "test-key")
    _mock_httpx(
        monkeypatch,
        lambda _r: httpx.Response(
            200,
            json={
                "id": "asset_123",
                "status": "failed",
                "data": {
                    "error": {
                        "message": "body.prompt: content flagged"
                    }
                },
            },
        ),
    )
    out = await arcads_video_status_tool.handler(
        {"asset_id": "asset_123"}, ToolContext()
    )
    assert not out.is_error
    assert out.data["status"] == "failed"
    assert "content flagged" in out.content


# ── direct ArcadsClient behaviour ───────────────────────────────────


@pytest.mark.asyncio
async def test_client_raises_on_non_2xx(monkeypatch) -> None:
    _mock_httpx(
        monkeypatch,
        lambda _r: httpx.Response(422, text="Validation"),
    )
    client = ArcadsClient(api_key="x")
    with pytest.raises(ArcadsError) as exc:
        await client.list_products()
    assert exc.value.status == 422


@pytest.mark.asyncio
async def test_client_uses_basic_auth_with_empty_password(
    monkeypatch,
) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("authorization") or "")
        return httpx.Response(200, json=[])

    _mock_httpx(monkeypatch, handler)
    client = ArcadsClient(api_key="the-key")
    await client.list_products()
    assert seen, "no request captured"
    # Basic base64 of "the-key:" is "dGhlLWtleTo="
    assert seen[0] == "Basic dGhlLWtleTo="
