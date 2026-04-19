"""AgentSDKEngine — Anthropic tool-use loop for coding drafts.

Middle-ground engine between the bare API (no filesystem access) and
the Claude Code CLI (subscription-billed, local-only). Runs an
Anthropic tool-use loop with a single read-only tool — ``fs_read`` —
so the model can actually look at files before drafting a response.
No writes, no shell: anything that mutates the repo is drafted in
text and applied later through the normal approval queue.

Why this middle ground is useful:
- The bare API engine answers blind ("Here's how you'd refactor X…"
  without ever seeing X).
- Claude Code edits files but only works on the operator's laptop
  where the CLI is installed — unavailable on Railway cloud.
- AgentSDKEngine sits between: informed drafts, runs everywhere the
  Anthropic client does, still per-token billed.

The tool-use loop is deliberately simple: max ``MAX_TURNS`` round
trips, single tool (``fs_read``), plain text output. Any upstream
failure returns a clean ``is_error`` result instead of raising so
the router can fall through to APIEngine.
"""

from __future__ import annotations

import anthropic

from core.coding.base import CodeRunResult, CodeTask, EngineHealth
from core.logging import get_logger
from core.tools.registry import ToolContext

log = get_logger("pilkd.coding.agent_sdk")

MAX_TURNS = 6
MAX_TOKENS = 4096

SYSTEM_PROMPT = (
    "You are PILK's code-drafting engine with file read access.\n\n"
    "Use the fs_read tool when you need to look at a file before "
    "answering — for example, when the user asks about existing code "
    "in the workspace. Do not guess at file contents; read them.\n\n"
    "Do not modify files. Return the smallest helpful code block plus a "
    "short explanation. PILK routes real edits through its approval "
    "queue separately.\n\n"
    "Keep prose tight. When you've done enough reading, finish with a "
    "final text response."
)


class AgentSDKEngine:
    name = "agent-sdk"
    label = "Anthropic Agent SDK"

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None,
        model: str,
    ) -> None:
        self._client = client
        self._model = model

    async def available(self) -> bool:
        return self._client is not None

    async def health(self) -> EngineHealth:
        if self._client is None:
            return EngineHealth(
                name=self.name,
                label=self.label,
                available=False,
                detail="ANTHROPIC_API_KEY not set",
            )
        return EngineHealth(
            name=self.name,
            label=self.label,
            available=True,
            detail=f"model: {self._model} · tools: fs_read",
        )

    async def run(self, task: CodeTask) -> CodeRunResult:
        if self._client is None:
            return CodeRunResult(
                engine=self.name,
                ok=False,
                summary=(
                    "Agent SDK unavailable — no Anthropic API key set."
                ),
            )

        # Deferred import: core.tools.builtin indirectly imports
        # core.coding via the code_task tool, so loading fs_read at
        # module level creates a circular import. Fetching it here
        # (once per run) is cheap.
        from core.tools.builtin.fs import fs_read_tool

        tools = [
            {
                "name": fs_read_tool.name,
                "description": fs_read_tool.description,
                "input_schema": fs_read_tool.input_schema,
            }
        ]
        messages: list[dict] = [
            {"role": "user", "content": _user_prompt(task)}
        ]
        tool_ctx = ToolContext()  # no sandbox — shared workspace
        final_text = ""
        stop_reason: str | None = None

        for _turn in range(MAX_TURNS):
            try:
                resp = await self._client.messages.create(
                    model=self._model,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    tools=tools,
                    messages=messages,
                )
            except Exception as e:
                log.exception("agent_sdk_anthropic_call_failed")
                return CodeRunResult(
                    engine=self.name,
                    ok=False,
                    summary=(
                        f"Agent SDK failed: {type(e).__name__}: {e}"
                    ),
                )

            stop_reason = getattr(resp, "stop_reason", None)
            text_this_turn = _join_text_blocks(resp.content)
            if text_this_turn:
                final_text = text_this_turn
            if stop_reason != "tool_use":
                break

            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                break

            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for use in tool_uses:
                outcome = await fs_read_tool.handler(
                    dict(getattr(use, "input", {}) or {}),
                    tool_ctx,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": getattr(use, "id", ""),
                        "content": outcome.content,
                        "is_error": outcome.is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
        else:
            # MAX_TURNS without a terminal stop — return what we have.
            log.warning("agent_sdk_hit_turn_cap", turns=MAX_TURNS)

        if not final_text:
            return CodeRunResult(
                engine=self.name,
                ok=False,
                summary="Agent SDK produced no final text.",
                metadata={"stop_reason": stop_reason},
            )

        summary = final_text.splitlines()[0]
        return CodeRunResult(
            engine=self.name,
            ok=True,
            summary=f"Drafted with tool access: {summary[:120]}",
            detail=final_text,
            usd=0.0,  # provider usage attributed by the ledger elsewhere
            metadata={
                "model": self._model,
                "scope": task.scope,
                "stop_reason": stop_reason,
            },
        )


def _user_prompt(task: CodeTask) -> str:
    lines = [f"Scope: {task.scope}"]
    if task.repo_path is not None:
        lines.append(f"Repo: {task.repo_path}")
    lines.append("")
    lines.append(task.goal.strip())
    return "\n".join(lines)


def _join_text_blocks(blocks) -> str:
    parts: list[str] = []
    for b in blocks or []:
        if getattr(b, "type", None) == "text":
            parts.append(getattr(b, "text", ""))
    return "\n".join(parts).strip()
