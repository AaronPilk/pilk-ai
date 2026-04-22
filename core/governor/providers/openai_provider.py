"""OpenAI planner provider.

Runs the turn loop against OpenAI's Chat Completions endpoint with
native function-calling tools. The translation layer converts the
Anthropic-shaped turn history the orchestrator already uses into
OpenAI's expected shape and translates the response back. Orchestrator
stays provider-agnostic.

Uses httpx directly (no openai SDK dep) — mirrors the approach used by
the voice drivers. Adaptive thinking / prompt caching are Anthropic-
only concepts and are silently ignored here.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from core.governor.providers.base import (
    PlannerResponse,
    TextBlock,
    ToolUseBlock,
    UsageLike,
)
from core.logging import get_logger

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# OpenAI's Chat Completions caps the `tools` array at 128. Anthropic
# allows many more; the orchestrator registers 150+ tools today and
# will keep growing. When routing through OpenAI we clamp the list
# (see `_prioritised_cap`) — anything dropped is logged so the
# operator can decide whether to curate the allowlist at the
# orchestrator layer for OpenAI-tier traffic specifically.
OPENAI_MAX_TOOLS = 128

# Tools that are most useful in a free-chat turn. Preserved ahead of
# the alphabetical tail when the registry overflows 128 entries. The
# literal list is fine here — we only need a stable priority for the
# ~10 generic primitives the LLM reaches for in every plan.
_CORE_TOOL_NAMES: tuple[str, ...] = (
    "fs_read",
    "fs_write",
    "shell_exec",
    "net_fetch",
    "llm_ask",
    "agent_create",
    "code_task",
    "timer_set",
    "memory_remember",
    "memory_list",
    "memory_delete",
    "brain_search",
    "brain_note_read",
    "brain_note_list",
    "brain_note_write",
    "brain_note_search_and_replace",
    "pilk_registered_tools",
    "pilk_recent_changes",
    "pilk_open_prs",
    "pilk_deploy_status",
)

# Prefixes to prioritise after the explicit list — keeps Gmail, Drive,
# Calendar, etc. alongside the generic builtins before the specialist
# trading / ads / creative surfaces.
_CORE_TOOL_PREFIXES: tuple[str, ...] = (
    "gmail_",
    "drive_",
    "calendar_",
    "sheets_",
    "slides_",
    "notion_",
    "ghl_",
)

log = get_logger("pilkd.governor.openai")


class OpenAIPlannerProvider:
    name = "openai"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

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
        _ = (use_thinking, cache_control)  # intentionally unused

        oai_messages = _translate_messages(system, messages)
        capped_tools = _prioritised_cap(tools, OPENAI_MAX_TOOLS)
        if len(capped_tools) < len(tools):
            dropped = [t["name"] for t in tools if t not in capped_tools]
            log.warning(
                "openai_tools_truncated",
                registered=len(tools),
                kept=len(capped_tools),
                dropped_count=len(dropped),
                # Log only the first few names — the full list would
                # balloon the structured log line on big registries.
                dropped_sample=dropped[:8],
                model=model,
            )
        oai_tools = _translate_tools(capped_tools)

        payload: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "max_completion_tokens": max_tokens,
        }
        if oai_tools:
            payload["tools"] = oai_tools
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if r.status_code >= 400:
                body_preview = r.text[:500] if r.text else ""
                raise RuntimeError(
                    f"openai {r.status_code} ({model}): {body_preview}"
                )
            data = r.json()

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        oai_finish = choice.get("finish_reason") or "stop"

        content: list[TextBlock | ToolUseBlock] = []
        txt = msg.get("content")
        if isinstance(txt, str) and txt.strip():
            content.append(TextBlock(text=txt))

        for tc in msg.get("tool_calls") or []:
            if tc.get("type") != "function":
                continue
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                parsed = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                log.warning(
                    "openai_tool_args_parse_failed",
                    model=model,
                    name=fn.get("name"),
                    raw=raw_args[:200],
                )
                parsed = {}
            content.append(
                ToolUseBlock(
                    id=tc.get("id") or "",
                    name=fn.get("name") or "",
                    input=parsed,
                )
            )

        stop_reason = "tool_use" if oai_finish == "tool_calls" else "end_turn"

        u = data.get("usage") or {}
        usage = UsageLike(
            input_tokens=int(u.get("prompt_tokens") or 0),
            output_tokens=int(u.get("completion_tokens") or 0),
            # OpenAI's prompt cache only reports when hit; cache_creation is
            # not surfaced separately. Map what we can.
            cache_creation_input_tokens=0,
            cache_read_input_tokens=int(
                (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            ),
        )
        return PlannerResponse(
            content=content,
            stop_reason=stop_reason,
            usage=usage,
            model=model,
        )


# ── translation helpers ──────────────────────────────────────────────


def _translate_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "parameters": t.get("input_schema")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _prioritised_cap(
    tools: list[dict[str, Any]], max_tools: int,
) -> list[dict[str, Any]]:
    """Keep up to `max_tools` tools, ordered so core primitives survive.

    Priority, highest first:
      1. Explicit core names (``fs_*``, ``shell_exec``, ``llm_ask``,
         brain / memory / pilk_* introspection, etc.).
      2. Tools whose name starts with a core prefix (Gmail, Drive,
         Calendar, Notion, GHL).
      3. Everything else, in the order the caller passed them in.

    The caller-order within each bucket is preserved so
    registry-sorted output stays alphabetical where possible (prompt
    caching stable across turns).
    """
    if len(tools) <= max_tools:
        return tools
    core_names = set(_CORE_TOOL_NAMES)
    prefixes = _CORE_TOOL_PREFIXES

    def priority(tool: dict[str, Any]) -> int:
        name = tool.get("name") or ""
        if name in core_names:
            return 0
        if name.startswith(prefixes):
            return 1
        return 2

    # Stable sort → ties preserve caller order.
    ordered = sorted(enumerate(tools), key=lambda ix: (priority(ix[1]), ix[0]))
    kept = [t for _, t in ordered[:max_tools]]
    return kept


def _translate_messages(
    system: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Translate Anthropic-shaped turn history to OpenAI format.

    Anthropic 'user' messages can be a string OR a list of content blocks
    (each either text or tool_result). Anthropic 'assistant' messages
    carry a list of blocks (text + tool_use). OpenAI expects:

    - system (role: system, string content)
    - user (role: user, string content)
    - assistant (role: assistant, content: str|null, tool_calls[]?)
    - tool   (role: tool, tool_call_id, content: str)  — one per tool result
    """
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for m in messages:
        role = m.get("role")
        content = m.get("content")

        if role == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            if isinstance(content, list):
                tool_results = [
                    b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                if tool_results:
                    for tr in tool_results:
                        out.append(
                            {
                                "role": "tool",
                                "tool_call_id": tr.get("tool_use_id") or "",
                                "content": _stringify(tr.get("content")),
                            }
                        )
                    continue
                # Plain text blocks from Anthropic: concatenate.
                text = "\n".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                out.append({"role": "user", "content": text})
            continue

        if role == "assistant":
            # Anthropic assistant content can be either raw blocks from the
            # SDK (objects with .type/.text/.input) or our own dicts. Handle
            # both.
            assistant_text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            iterable = content if isinstance(content, list) else []
            for b in iterable:
                btype = _btype(b)
                if btype == "text":
                    assistant_text_parts.append(_bget(b, "text", "") or "")
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "id": _bget(b, "id", "") or "",
                            "type": "function",
                            "function": {
                                "name": _bget(b, "name", "") or "",
                                "arguments": json.dumps(_bget(b, "input", {}) or {}),
                            },
                        }
                    )
            payload: dict[str, Any] = {"role": "assistant"}
            joined = "\n".join(t for t in assistant_text_parts if t).strip()
            payload["content"] = joined or None
            if tool_calls:
                payload["tool_calls"] = tool_calls
            out.append(payload)
            continue

    return out


def _btype(b: Any) -> str | None:
    if isinstance(b, dict):
        return b.get("type")
    return getattr(b, "type", None)


def _bget(b: Any, key: str, default: Any) -> Any:
    if isinstance(b, dict):
        return b.get(key, default)
    return getattr(b, key, default)


def _stringify(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return json.dumps(content)
