"""Planner provider abstraction.

The Governor picks a (provider, model) for each plan; the provider
executes the turn loop. A provider is a small interface: given the
system prompt, the turn history (Anthropic-shaped), the tool schemas,
and the model id, it returns a `PlannerResponse` whose shape mimics
the Anthropic Messages response — `.content` list of text / tool_use
blocks, a `.stop_reason`, and a `.usage` object with token counts.

The Anthropic provider is a thin passthrough. The OpenAI provider
translates Anthropic-shaped messages and tool results into the OpenAI
Chat Completions + tool_calls format, then translates the response
back so the orchestrator can stay provider-agnostic.

Prompt caching is Anthropic-only; the OpenAI provider ignores the
`cache_control` flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    type: str = "tool_use"


@dataclass
class UsageLike:
    """Duck-typed usage object compatible with UsageSnapshot.from_anthropic."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class PlannerResponse:
    content: list[TextBlock | ToolUseBlock]
    stop_reason: str
    usage: UsageLike
    model: str


class PlannerProvider(Protocol):
    name: str

    async def plan_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        max_tokens: int,
        use_thinking: bool,
        cache_control: bool,
    ) -> PlannerResponse:
        ...
