"""Unit tests for Anthropic prompt-cache wiring.

Regression test: prior to this suite, ``cache_control=True`` was sent
as a top-level request param — silently ignored by the SDK + API, so
every turn paid full price for the system prompt + tool schemas. Real
cost in the field was ~20x higher than it should have been.

These tests assert the wire-level shape Anthropic actually looks at:

- ``system`` becomes a list-of-content-blocks with
  ``cache_control={"type": "ephemeral"}`` on the (only) block.
- The LAST tool in the ``tools`` array carries the same annotation so
  it acts as the cache breakpoint for every preceding tool.
- ``cache_control=False`` leaves both as plain string + unchanged
  list — no annotations sneak in.
- The caller's ``tools`` list is never mutated; we copy.
- The top-level request dict does NOT contain a ``cache_control``
  key (that was the original bug).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from core.governor.providers import AnthropicPlannerProvider


@dataclass
class _Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict | None = None


@dataclass
class _Usage:
    input_tokens: int = 100
    output_tokens: int = 20
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _Response:
    content: list[_Block] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)


class _CapturingMessages:
    """Captures every kwargs dict passed to messages.create."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = _Response(
            content=[_Block(type="text", text="ok")],
        )

    async def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self.response


class _CapturingClient:
    def __init__(self) -> None:
        self.messages = _CapturingMessages()


SYSTEM_PROMPT = "You are PILK. Do the thing."
TOOL_A = {
    "name": "fs_read",
    "description": "read a file",
    "input_schema": {"type": "object"},
}
TOOL_B = {
    "name": "fs_write",
    "description": "write a file",
    "input_schema": {"type": "object"},
}
TOOL_C = {
    "name": "shell_exec",
    "description": "run shell",
    "input_schema": {"type": "object"},
}


# ── cache_control=True ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_on_wraps_system_in_annotated_block() -> None:
    client = _CapturingClient()
    provider = AnthropicPlannerProvider(client)  # type: ignore[arg-type]
    await provider.plan_turn(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "hi"}],
        tools=[TOOL_A, TOOL_B, TOOL_C],
        model="claude-haiku-4-5",
        max_tokens=1024,
        use_thinking=False,
        cache_control=True,
    )
    req = client.messages.calls[0]
    system = req["system"]
    assert isinstance(system, list), (
        "system must be a list of content blocks when caching is on"
    )
    assert len(system) == 1
    assert system[0]["type"] == "text"
    assert system[0]["text"] == SYSTEM_PROMPT
    assert system[0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_on_annotates_last_tool_only() -> None:
    client = _CapturingClient()
    provider = AnthropicPlannerProvider(client)  # type: ignore[arg-type]
    await provider.plan_turn(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "hi"}],
        tools=[TOOL_A, TOOL_B, TOOL_C],
        model="claude-haiku-4-5",
        max_tokens=1024,
        use_thinking=False,
        cache_control=True,
    )
    req = client.messages.calls[0]
    sent_tools = req["tools"]
    assert len(sent_tools) == 3
    # First two must be clean — cache_control only on the last.
    assert "cache_control" not in sent_tools[0]
    assert "cache_control" not in sent_tools[1]
    # Last tool carries the annotation.
    assert sent_tools[2]["cache_control"] == {"type": "ephemeral"}
    # The rest of the last tool's fields are preserved.
    assert sent_tools[2]["name"] == TOOL_C["name"]
    assert sent_tools[2]["description"] == TOOL_C["description"]


@pytest.mark.asyncio
async def test_cache_on_does_not_set_top_level_cache_control() -> None:
    """This is the actual regression. Prior code set
    ``req['cache_control']`` — the API silently ignores it. The new
    code must NOT do that; annotations live on content blocks only."""
    client = _CapturingClient()
    provider = AnthropicPlannerProvider(client)  # type: ignore[arg-type]
    await provider.plan_turn(
        system=SYSTEM_PROMPT,
        messages=[],
        tools=[TOOL_A],
        model="claude-haiku-4-5",
        max_tokens=1024,
        use_thinking=False,
        cache_control=True,
    )
    req = client.messages.calls[0]
    assert "cache_control" not in req, (
        "Top-level cache_control in the request body is ignored by "
        "Anthropic. The old bug was passing it here. Annotations "
        "belong on system / tool content blocks."
    )


@pytest.mark.asyncio
async def test_cache_on_does_not_mutate_caller_tools() -> None:
    """Callers pass a registry-derived list; we shouldn't mutate it
    under them. The annotated tool must be a COPY, not the same dict
    reference the caller gave us."""
    client = _CapturingClient()
    provider = AnthropicPlannerProvider(client)  # type: ignore[arg-type]
    original_tools = [dict(TOOL_A), dict(TOOL_B)]  # fresh copies for the
                                                   # assertion below to be
                                                   # meaningful
    original_last_id = id(original_tools[-1])
    await provider.plan_turn(
        system=SYSTEM_PROMPT,
        messages=[],
        tools=original_tools,
        model="claude-haiku-4-5",
        max_tokens=1024,
        use_thinking=False,
        cache_control=True,
    )
    # The caller's last tool dict must be unchanged.
    assert "cache_control" not in original_tools[-1]
    assert id(original_tools[-1]) == original_last_id
    # And the outbound list contains a DIFFERENT dict for the last
    # slot (the annotated copy).
    req = client.messages.calls[0]
    assert id(req["tools"][-1]) != original_last_id


@pytest.mark.asyncio
async def test_cache_on_handles_empty_tools_list() -> None:
    """An agent with zero tools should still set the system cache
    annotation without crashing or sneaking in an invalid empty-tool
    annotation."""
    client = _CapturingClient()
    provider = AnthropicPlannerProvider(client)  # type: ignore[arg-type]
    await provider.plan_turn(
        system=SYSTEM_PROMPT,
        messages=[],
        tools=[],
        model="claude-haiku-4-5",
        max_tokens=1024,
        use_thinking=False,
        cache_control=True,
    )
    req = client.messages.calls[0]
    assert req["tools"] == []
    # System still annotated.
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}


# ── cache_control=False ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_off_keeps_system_as_plain_string() -> None:
    client = _CapturingClient()
    provider = AnthropicPlannerProvider(client)  # type: ignore[arg-type]
    await provider.plan_turn(
        system=SYSTEM_PROMPT,
        messages=[],
        tools=[TOOL_A, TOOL_B],
        model="claude-haiku-4-5",
        max_tokens=1024,
        use_thinking=False,
        cache_control=False,
    )
    req = client.messages.calls[0]
    # String, not a list — backwards-compatible with the pre-fix path
    # for callers that have their own reason to skip caching.
    assert req["system"] == SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_cache_off_does_not_annotate_tools() -> None:
    client = _CapturingClient()
    provider = AnthropicPlannerProvider(client)  # type: ignore[arg-type]
    await provider.plan_turn(
        system=SYSTEM_PROMPT,
        messages=[],
        tools=[TOOL_A, TOOL_B, TOOL_C],
        model="claude-haiku-4-5",
        max_tokens=1024,
        use_thinking=False,
        cache_control=False,
    )
    req = client.messages.calls[0]
    for t in req["tools"]:
        assert "cache_control" not in t


# ── thinking flag still works ──────────────────────────────────


@pytest.mark.asyncio
async def test_thinking_enabled_only_for_opus() -> None:
    """The cache_control rewrite shouldn't accidentally drop the
    adaptive-thinking path. Opus gets thinking; other models don't."""
    client = _CapturingClient()
    provider = AnthropicPlannerProvider(client)  # type: ignore[arg-type]
    await provider.plan_turn(
        system=SYSTEM_PROMPT,
        messages=[],
        tools=[TOOL_A],
        model="claude-opus-4-7",
        max_tokens=1024,
        use_thinking=True,
        cache_control=True,
    )
    req = client.messages.calls[0]
    assert req["thinking"] == {"type": "adaptive"}


@pytest.mark.asyncio
async def test_thinking_not_set_on_sonnet_even_when_requested() -> None:
    client = _CapturingClient()
    provider = AnthropicPlannerProvider(client)  # type: ignore[arg-type]
    await provider.plan_turn(
        system=SYSTEM_PROMPT,
        messages=[],
        tools=[TOOL_A],
        model="claude-sonnet-4-6",
        max_tokens=1024,
        use_thinking=True,
        cache_control=True,
    )
    req = client.messages.calls[0]
    assert "thinking" not in req


# ── usage surfacing ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_surfaces_in_usage() -> None:
    """When the API reports cache_read_input_tokens on a response,
    the provider must pass that number through to the orchestrator —
    the cost ledger reads it to bill cached reads at the discounted
    rate."""
    client = _CapturingClient()
    client.messages.response = _Response(
        content=[_Block(type="text", text="cached")],
        usage=_Usage(
            input_tokens=200,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=18_000,
        ),
    )
    provider = AnthropicPlannerProvider(client)  # type: ignore[arg-type]
    resp = await provider.plan_turn(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "again"}],
        tools=[TOOL_A],
        model="claude-haiku-4-5",
        max_tokens=1024,
        use_thinking=False,
        cache_control=True,
    )
    assert resp.usage.cache_read_input_tokens == 18_000
    assert resp.usage.cache_creation_input_tokens == 0
