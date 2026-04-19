"""Unit tests for the creative-content toolkit.

Network calls are stubbed via ``httpx.MockTransport``. Each test
exercises one of:

- input validation (missing prompt, bad aspect ratio)
- "not configured" error paths (no API key)
- happy-path: upstream JSON → bytes saved to sandbox workspace
- upstream error surfacing (4xx/5xx)
- Higgsfield polling: success after one in-progress tick, server-
  reported failure, and client-side timeout.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from core.config import get_settings
from core.tools.builtin import creative
from core.tools.builtin.creative import (
    higgsfield_generate_tool,
    nano_banana_generate_tool,
)
from core.tools.registry import ToolContext

SAMPLE_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-bytes"
SAMPLE_MP4_BYTES = b"\x00\x00\x00\x20ftypmp42" + b"x" * 64


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


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(sandbox_root=tmp_path)


# ── nano_banana_generate ────────────────────────────────────────


@pytest.mark.asyncio
async def test_nano_banana_requires_prompt(ctx: ToolContext) -> None:
    out = await nano_banana_generate_tool.handler({"prompt": ""}, ctx)
    assert out.is_error
    assert "prompt" in out.content.lower()


@pytest.mark.asyncio
async def test_nano_banana_bad_aspect_ratio(ctx: ToolContext) -> None:
    out = await nano_banana_generate_tool.handler(
        {"prompt": "sunset", "aspect_ratio": "bogus"}, ctx
    )
    assert out.is_error
    assert "aspect" in out.content.lower()


@pytest.mark.asyncio
async def test_nano_banana_missing_key(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    get_settings.cache_clear()
    for key in (
        "NANO_BANANA_API_KEY",
        "PILK_NANO_BANANA_API_KEY",
        "GEMINI_API_KEY",
        "PILK_GEMINI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    out = await nano_banana_generate_tool.handler(
        {"prompt": "a hero shot of a kitchen remodel"}, ctx
    )
    assert out.is_error
    assert "nano banana" in out.content.lower()


@pytest.mark.asyncio
async def test_nano_banana_happy_path(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("NANO_BANANA_API_KEY", "test-key")

    def handler(req: httpx.Request) -> httpx.Response:
        assert "generativelanguage.googleapis.com" in str(req.url)
        assert "gemini-2.5-flash-image" in str(req.url)
        assert req.headers.get("x-goog-api-key") == "test-key"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(
                                            SAMPLE_PNG_BYTES
                                        ).decode(),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    _install_transport(monkeypatch, handler)
    out = await nano_banana_generate_tool.handler(
        {"prompt": "sunset over Tampa", "aspect_ratio": "16:9"}, ctx
    )
    assert not out.is_error, out.content
    rel = out.data["path"]
    assert rel.startswith("creative/")
    abs_path = Path(out.data["absolute_path"])
    assert abs_path.exists()
    assert abs_path.read_bytes() == SAMPLE_PNG_BYTES
    assert out.data["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_nano_banana_upstream_400(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("NANO_BANANA_API_KEY", "test-key")
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(400, json={"error": "bad prompt"}),
    )
    out = await nano_banana_generate_tool.handler(
        {"prompt": "x"}, ctx
    )
    assert out.is_error
    assert "400" in out.content


@pytest.mark.asyncio
async def test_nano_banana_empty_response(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("NANO_BANANA_API_KEY", "test-key")
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"candidates": []}),
    )
    out = await nano_banana_generate_tool.handler(
        {"prompt": "anything"}, ctx
    )
    assert out.is_error
    assert "no image" in out.content.lower()


# ── higgsfield_generate ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_higgsfield_requires_prompt(ctx: ToolContext) -> None:
    out = await higgsfield_generate_tool.handler({"prompt": ""}, ctx)
    assert out.is_error
    assert "prompt" in out.content.lower()


@pytest.mark.asyncio
async def test_higgsfield_missing_key(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    get_settings.cache_clear()
    for key in ("HIGGSFIELD_API_KEY", "PILK_HIGGSFIELD_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    out = await higgsfield_generate_tool.handler(
        {"prompt": "a walking CPA"}, ctx
    )
    assert out.is_error
    assert "higgsfield" in out.content.lower()


@pytest.mark.asyncio
async def test_higgsfield_happy_path(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "hf-test")
    # Kill the poll interval so the test doesn't actually sleep 5s.
    monkeypatch.setattr(creative, "HIGGSFIELD_POLL_INTERVAL_S", 0.0)
    poll_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        method = req.method.upper()
        if method == "POST" and url.endswith("/v1/generations"):
            assert req.headers.get("authorization") == "Bearer hf-test"
            return httpx.Response(
                200,
                json={
                    "generation_id": "gen-123",
                    "status_url": "https://platform.higgsfield.ai/v1/generations/gen-123",
                },
            )
        if method == "GET" and url.endswith("/v1/generations/gen-123"):
            poll_count["n"] += 1
            if poll_count["n"] == 1:
                return httpx.Response(200, json={"status": "running"})
            return httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "video_url": "https://cdn.example/vid.mp4",
                },
            )
        if method == "GET" and url.endswith("vid.mp4"):
            return httpx.Response(200, content=SAMPLE_MP4_BYTES)
        return httpx.Response(404, text=f"unexpected {method} {url}")

    _install_transport(monkeypatch, handler)
    out = await higgsfield_generate_tool.handler(
        {"prompt": "golden-hour coastline, smooth cam", "duration_s": 6},
        ctx,
    )
    assert not out.is_error, out.content
    assert out.data["generation_id"] == "gen-123"
    assert out.data["duration_s"] == 6
    assert poll_count["n"] == 2
    abs_path = Path(out.data["absolute_path"])
    assert abs_path.exists()
    assert abs_path.read_bytes() == SAMPLE_MP4_BYTES


@pytest.mark.asyncio
async def test_higgsfield_server_reported_failure(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "hf-test")
    monkeypatch.setattr(creative, "HIGGSFIELD_POLL_INTERVAL_S", 0.0)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method.upper() == "POST":
            return httpx.Response(
                200,
                json={
                    "generation_id": "gen-fail",
                    "status_url": "https://platform.higgsfield.ai/v1/generations/gen-fail",
                },
            )
        return httpx.Response(
            200,
            json={"status": "failed", "error": "content policy violation"},
        )

    _install_transport(monkeypatch, handler)
    out = await higgsfield_generate_tool.handler(
        {"prompt": "x"}, ctx
    )
    assert out.is_error
    assert "failed" in out.content.lower()


@pytest.mark.asyncio
async def test_higgsfield_poll_timeout(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "hf-test")
    # Force the wait-cap to zero so the loop exits before any poll.
    monkeypatch.setattr(creative, "HIGGSFIELD_MAX_WAIT_S", 0.0)
    monkeypatch.setattr(creative, "HIGGSFIELD_POLL_INTERVAL_S", 0.0)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method.upper() == "POST":
            return httpx.Response(
                200,
                json={
                    "generation_id": "gen-slow",
                    "status_url": "https://platform.higgsfield.ai/v1/generations/gen-slow",
                },
            )
        return httpx.Response(200, json={"status": "running"})

    _install_transport(monkeypatch, handler)
    out = await higgsfield_generate_tool.handler(
        {"prompt": "slow render"}, ctx
    )
    assert out.is_error
    assert "timed out" in out.content.lower()


@pytest.mark.asyncio
async def test_higgsfield_image_to_video_mode(
    monkeypatch: pytest.MonkeyPatch, ctx: ToolContext
) -> None:
    """When `image_url` is provided, it should be forwarded to the
    create endpoint so Higgsfield switches modes."""
    get_settings.cache_clear()
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "hf-test")
    monkeypatch.setattr(creative, "HIGGSFIELD_POLL_INTERVAL_S", 0.0)

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method.upper() == "POST":
            import json as _json

            seen["body"] = _json.loads(req.content)
            return httpx.Response(
                200,
                json={
                    "generation_id": "gen-iv",
                    "status_url": "https://platform.higgsfield.ai/v1/generations/gen-iv",
                },
            )
        url = str(req.url)
        if url.endswith("/v1/generations/gen-iv"):
            return httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "video_url": "https://cdn.example/clip.mp4",
                },
            )
        return httpx.Response(200, content=SAMPLE_MP4_BYTES)

    _install_transport(monkeypatch, handler)
    out = await higgsfield_generate_tool.handler(
        {
            "prompt": "camera tracks forward",
            "image_url": "https://cdn.example/ref.png",
            "duration_s": 4,
        },
        ctx,
    )
    assert not out.is_error, out.content
    assert seen["body"]["image_url"] == "https://cdn.example/ref.png"
    assert seen["body"]["duration"] == 4
