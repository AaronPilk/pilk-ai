"""Multi-provider routing: OpenAI / Gemini / Grok share one code path.

The three share the OpenAI Chat Completions shape, so a single provider
class backs all of them with different ``base_url`` + ``name`` values.
This module exercises that the endpoint switches correctly and that
``build_providers`` wires each one in when its key is set.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from core.governor.providers import build_providers
from core.governor.providers.openai_provider import (
    GEMINI_BASE_URL,
    GROK_BASE_URL,
    OPENAI_BASE_URL,
    OpenAIPlannerProvider,
)


def _mock_ok_response() -> dict[str, Any]:
    """Minimal valid Chat Completions response — text-only, no tools."""
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


def _install_httpx_capture(captured: list[httpx.Request]) -> None:
    real_init = httpx.AsyncClient.__init__

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json=_mock_ok_response())

    transport = httpx.MockTransport(handler)

    def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _restore_httpx_init():
    original = httpx.AsyncClient.__init__
    yield
    httpx.AsyncClient.__init__ = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_default_constructor_targets_openai() -> None:
    captured: list[httpx.Request] = []
    _install_httpx_capture(captured)

    provider = OpenAIPlannerProvider("sk-test")
    assert provider.name == "openai"

    await provider.plan_turn(
        system="s", messages=[{"role": "user", "content": "hi"}],
        tools=[], model="gpt-4o-mini", max_tokens=16,
        use_thinking=False, cache_control=False,
    )

    assert len(captured) == 1
    assert str(captured[0].url) == f"{OPENAI_BASE_URL}/chat/completions"


@pytest.mark.asyncio
async def test_gemini_base_url_overrides_endpoint() -> None:
    captured: list[httpx.Request] = []
    _install_httpx_capture(captured)

    provider = OpenAIPlannerProvider(
        "gemini-test", base_url=GEMINI_BASE_URL, name="gemini",
    )
    assert provider.name == "gemini"

    await provider.plan_turn(
        system="s", messages=[{"role": "user", "content": "hi"}],
        tools=[], model="gemini-2.5-flash", max_tokens=16,
        use_thinking=False, cache_control=False,
    )

    assert str(captured[0].url) == f"{GEMINI_BASE_URL}/chat/completions"
    assert captured[0].headers["Authorization"] == "Bearer gemini-test"


@pytest.mark.asyncio
async def test_grok_base_url_overrides_endpoint() -> None:
    captured: list[httpx.Request] = []
    _install_httpx_capture(captured)

    provider = OpenAIPlannerProvider(
        "xai-test", base_url=GROK_BASE_URL, name="grok",
    )
    assert provider.name == "grok"

    await provider.plan_turn(
        system="s", messages=[{"role": "user", "content": "hi"}],
        tools=[], model="grok-4-fast", max_tokens=16,
        use_thinking=False, cache_control=False,
    )

    assert str(captured[0].url) == f"{GROK_BASE_URL}/chat/completions"


@pytest.mark.asyncio
async def test_base_url_trailing_slash_is_stripped() -> None:
    """Caller might paste a URL ending with `/` — we should cope."""
    captured: list[httpx.Request] = []
    _install_httpx_capture(captured)

    provider = OpenAIPlannerProvider(
        "k", base_url="https://example.test/v1/", name="whatever",
    )
    await provider.plan_turn(
        system="s", messages=[{"role": "user", "content": "hi"}],
        tools=[], model="m", max_tokens=16,
        use_thinking=False, cache_control=False,
    )
    assert str(captured[0].url) == "https://example.test/v1/chat/completions"


def test_build_providers_registers_openai_when_key_set() -> None:
    providers = build_providers(
        anthropic_client=None, openai_api_key="sk-oai",
        enable_claude_code_chat=False,
    )
    assert "openai" in providers
    assert providers["openai"].name == "openai"
    assert "gemini" not in providers
    assert "grok" not in providers


def test_build_providers_registers_gemini_and_grok_when_keys_set() -> None:
    providers = build_providers(
        anthropic_client=None, openai_api_key=None,
        gemini_api_key="gem-xyz", grok_api_key="xai-abc",
        enable_claude_code_chat=False,
    )
    assert "gemini" in providers
    assert providers["gemini"].name == "gemini"
    assert "grok" in providers
    assert providers["grok"].name == "grok"
    assert "openai" not in providers


def test_build_providers_skips_providers_with_no_key() -> None:
    providers = build_providers(
        anthropic_client=None, openai_api_key=None,
        gemini_api_key=None, grok_api_key=None,
        enable_claude_code_chat=False,
    )
    assert providers == {}


def test_build_providers_all_four_llm_providers_coexist() -> None:
    """Anthropic, OpenAI, Gemini, and Grok all registered together."""

    class _StubAnthropic:
        pass

    providers = build_providers(
        anthropic_client=_StubAnthropic(),  # type: ignore[arg-type]
        openai_api_key="sk-o",
        gemini_api_key="k-g",
        grok_api_key="k-x",
        enable_claude_code_chat=False,
    )
    assert set(providers.keys()) == {"anthropic", "openai", "gemini", "grok"}
