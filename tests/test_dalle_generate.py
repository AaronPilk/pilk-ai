"""DALL-E 3 image-gen tool — OpenAI images API wrapper.

Network stubbed via httpx.MockTransport so the test suite makes no
real API calls. Covers the happy path + all three error branches:
missing key, HTTP error, missing b64 in payload.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from core.tools.builtin.creative import (
    DALLE_ASPECT_TO_SIZE,
    DALLE_MODEL,
    OPENAI_IMAGES_URL,
    _dalle_generate,
    dalle_generate_tool,
)
from core.tools.registry import ToolContext

# A 1x1 transparent PNG as test payload — tiny but valid image bytes.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "89000000017352474200aece1ce90000000d4944415478da6360600000000400"
    "01e5270a1a0000000049454e44ae426082"
)


def _install_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _restore_httpx_init():
    original = httpx.AsyncClient.__init__
    yield
    httpx.AsyncClient.__init__ = original  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _with_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-abc")
    # Clear any cached settings so the key takes effect.
    from core.config import settings as _settings

    _settings.get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    _settings.get_settings.cache_clear()  # type: ignore[attr-defined]


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(sandbox_root=tmp_path)


# ── happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dalle_generate_writes_png_to_workspace(tmp_path: Path) -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == OPENAI_IMAGES_URL
        body = json.loads(req.content.decode())
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(_TINY_PNG).decode(),
                        "revised_prompt": "a sharper version of your prompt",
                    }
                ]
            },
        )

    _install_transport(handler)

    result = await _dalle_generate(
        {"prompt": "cozy cabin at dusk", "aspect_ratio": "16:9"},
        _ctx(tmp_path),
    )

    assert result.is_error is False
    # Right model + request shape went out.
    body = captured[0]
    assert body["model"] == DALLE_MODEL
    assert body["size"] == DALLE_ASPECT_TO_SIZE["16:9"]
    assert body["quality"] == "standard"
    assert body["n"] == 1
    assert body["response_format"] == "b64_json"
    # File landed in workspace with PNG bytes.
    written = Path(result.data["absolute_path"])
    assert written.exists()
    assert written.read_bytes() == _TINY_PNG
    assert result.data["model"] == DALLE_MODEL
    assert result.data["revised_prompt"].startswith("a sharper")


@pytest.mark.asyncio
async def test_dalle_generate_hd_quality_flag(tmp_path: Path) -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content.decode()))
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(_TINY_PNG).decode()}]},
        )

    _install_transport(handler)
    result = await _dalle_generate(
        {"prompt": "hd test", "quality": "hd"},
        _ctx(tmp_path),
    )
    assert result.is_error is False
    assert captured[0]["quality"] == "hd"


# ── validation errors (no network touched) ──────────────────────


@pytest.mark.asyncio
async def test_dalle_generate_requires_prompt(tmp_path: Path) -> None:
    result = await _dalle_generate({}, _ctx(tmp_path))
    assert result.is_error is True
    assert "prompt" in result.content


@pytest.mark.asyncio
async def test_dalle_generate_rejects_unsupported_aspect(tmp_path: Path) -> None:
    result = await _dalle_generate(
        {"prompt": "x", "aspect_ratio": "4:3"},
        _ctx(tmp_path),
    )
    assert result.is_error is True
    assert "4:3" in result.content


@pytest.mark.asyncio
async def test_dalle_generate_rejects_unknown_quality(tmp_path: Path) -> None:
    result = await _dalle_generate(
        {"prompt": "x", "quality": "ultra"},
        _ctx(tmp_path),
    )
    assert result.is_error is True
    assert "quality" in result.content.lower()


# ── missing credential ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_dalle_generate_missing_api_key(
    tmp_path: Path, monkeypatch,
) -> None:
    # Clearing the env var alone isn't enough: ``_dalle_generate``
    # resolves through ``_secret`` which checks the dashboard-stored
    # secret first (lives on disk, survives env clears). Stub the
    # whole helper so this test really exercises the no-key branch.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from core.config import settings as _settings
    from core.tools.builtin import creative as _creative

    _settings.get_settings.cache_clear()  # type: ignore[attr-defined]
    monkeypatch.setattr(
        _creative, "_secret", lambda name, fallback: None
    )

    result = await _dalle_generate(
        {"prompt": "x"}, _ctx(tmp_path),
    )
    assert result.is_error is True
    assert "OPENAI_API_KEY" in result.content


# ── remote errors / bad payloads ────────────────────────────────


@pytest.mark.asyncio
async def test_dalle_generate_http_error(tmp_path: Path) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "rate limited"}},
            text='{"error":{"message":"rate limited"}}',
        )

    _install_transport(handler)
    result = await _dalle_generate(
        {"prompt": "x"}, _ctx(tmp_path),
    )
    assert result.is_error is True
    assert "429" in result.content


@pytest.mark.asyncio
async def test_dalle_generate_missing_image_bytes(tmp_path: Path) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    _install_transport(handler)
    result = await _dalle_generate(
        {"prompt": "x"}, _ctx(tmp_path),
    )
    assert result.is_error is True
    assert "no image bytes" in result.content.lower()


# ── tool registration ──────────────────────────────────────────


def test_dalle_tool_shape() -> None:
    """Schema surface the LLM sees is stable — prompt required, aspect
    + quality optional, risk class NET_WRITE."""
    assert dalle_generate_tool.name == "dalle_generate"
    assert dalle_generate_tool.risk.name == "NET_WRITE"
    schema = dalle_generate_tool.input_schema
    assert schema["required"] == ["prompt"]
    assert "aspect_ratio" in schema["properties"]
    assert "quality" in schema["properties"]
