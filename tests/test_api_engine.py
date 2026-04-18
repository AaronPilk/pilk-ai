"""APIEngine — smoke test the draft-only tool-less loop with a stubbed client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from core.coding import APIEngine, CodeTask


@dataclass
class _Block:
    type: str
    text: str = ""


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _Response:
    content: list[_Block]
    stop_reason: str = "end_turn"
    usage: _Usage = None  # type: ignore[assignment]


class _StubMessages:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _StubClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.messages = _StubMessages(responses)


@pytest.mark.asyncio
async def test_api_engine_unavailable_without_client() -> None:
    engine = APIEngine(client=None, model="claude-haiku-4-5")
    assert await engine.available() is False
    health = await engine.health()
    assert health.available is False
    assert "ANTHROPIC_API_KEY" in health.detail


@pytest.mark.asyncio
async def test_api_engine_drafts_a_response() -> None:
    client = _StubClient(
        [
            _Response(
                content=[_Block(type="text", text="Here is a helper.\n\ndef x(): ...")],
                usage=_Usage(input_tokens=30, output_tokens=12),
            ),
        ],
    )
    engine = APIEngine(client=client, model="claude-haiku-4-5")  # type: ignore[arg-type]

    assert await engine.available() is True
    result = await engine.run(CodeTask(goal="write me a helper", scope="function"))
    assert result.ok is True
    assert result.engine == "api"
    assert "Here is a helper." in result.detail
    assert result.summary.startswith("Drafted a response")
    # Stub captured the call with the right model.
    assert client.messages.calls[0]["model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_api_engine_returns_ok_false_on_exception() -> None:
    class _BoomMessages:
        async def create(self, **_kwargs):
            raise RuntimeError("transport down")

    class _BoomClient:
        messages = _BoomMessages()

    engine = APIEngine(client=_BoomClient(), model="claude-haiku-4-5")  # type: ignore[arg-type]
    result = await engine.run(CodeTask(goal="x", scope="function"))
    assert result.ok is False
    assert "transport down" in result.summary
