"""Anthropic planner provider.

Thin wrapper around Anthropic's Messages API. Prompt caching and
adaptive thinking (Opus only) are the two provider-specific flags we
surface here. The response is normalized into the shared PlannerResponse
shape so the orchestrator doesn't care who answered.
"""

from __future__ import annotations

from typing import Any

import anthropic

from core.governor.providers.base import (
    PlannerResponse,
    TextBlock,
    ToolUseBlock,
    UsageLike,
)


def _supports_thinking(model: str) -> bool:
    return "opus" in (model or "").lower()


class AnthropicPlannerProvider:
    name = "anthropic"

    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        self._client = client

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
        req: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
        }
        if cache_control:
            req["cache_control"] = {"type": "ephemeral"}
        if use_thinking and _supports_thinking(model):
            req["thinking"] = {"type": "adaptive"}

        response = await self._client.messages.create(**req)

        content: list[TextBlock | ToolUseBlock] = []
        for b in response.content:
            kind = getattr(b, "type", None)
            if kind == "text":
                content.append(TextBlock(text=b.text))
            elif kind == "tool_use":
                content.append(
                    ToolUseBlock(id=b.id, name=b.name, input=dict(b.input or {}))
                )
            # Other block types (thinking, image) are dropped — orchestrator
            # only cares about text + tool_use.

        u = response.usage
        usage = UsageLike(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0)
            or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        )
        return PlannerResponse(
            content=content,
            stop_reason=response.stop_reason or "end_turn",
            usage=usage,
            model=model,
        )
