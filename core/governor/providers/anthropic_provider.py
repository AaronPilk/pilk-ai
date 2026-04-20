"""Anthropic planner provider.

Thin wrapper around Anthropic's Messages API. Prompt caching and
adaptive thinking (Opus only) are the two provider-specific flags we
surface here. The response is normalized into the shared PlannerResponse
shape so the orchestrator doesn't care who answered.

### Prompt caching

Anthropic caches on per-block ``cache_control`` annotations — NOT on a
top-level request parameter. Prior to this module's rewrite we set
``req["cache_control"] = {"type": "ephemeral"}`` which the SDK silently
dropped and the API ignored, so every turn paid full price for the
~20-25K-token system prompt + all tool schemas.

We now create two cache breakpoints:

1. the end of the system content block (stable PILK-wide guidance)
2. the end of the tools array (stable tool registry)

Every subsequent turn that reuses those same bytes reads them at 10%
of creation cost. For the top-level chat loop that's the difference
between ~$0.08 and ~$0.008 per turn on Sonnet, and scales further on
Opus.

Two caveats worth flagging:

* Caching is all-or-nothing per block — if a tool's description
  changes between calls, its block fails to read-hit and falls back
  to a full create. Tool schemas are deterministic so this should be
  stable across a running daemon; a registry-mutation follow-up will
  break cache until the fresh shape is re-seeded.
* The last tool in the list is the only one we annotate (saving
  annotation slots for future use), but annotating the last block
  caches EVERY preceding block too.
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
            "messages": messages,
        }
        if cache_control:
            # System as a list-of-content-blocks with the last (only)
            # block carrying cache_control. Annotating a block caches
            # itself and every preceding block of the same kind, which
            # is fine here since we have exactly one.
            req["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            # Tools are a separate cache domain. Annotate the LAST tool
            # so it + every preceding tool is cached together. Copy
            # rather than mutate so the caller's registry-derived list
            # stays pristine between calls.
            if tools:
                tools_annotated = list(tools)
                tools_annotated[-1] = {
                    **tools_annotated[-1],
                    "cache_control": {"type": "ephemeral"},
                }
                req["tools"] = tools_annotated
            else:
                req["tools"] = tools
        else:
            req["system"] = system
            req["tools"] = tools
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
