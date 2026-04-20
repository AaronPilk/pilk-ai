"""Claude Code CLI planner provider — subscription-backed chat.

The Anthropic API provider bills every token against the operator's
API credits. For LIGHT-tier chat (greetings, meta-questions, quick
Q&A), that's pure waste when the operator already pays for Claude
Max / Pro — which includes Claude Code CLI usage at **zero marginal
API cost.**

This provider shells out to the ``claude`` CLI in print-mode
(``-p --output-format json``) so the operator's subscription covers
the call. Every LIGHT-tier turn that lands here is effectively free
compared to the same turn on the API path.

### The honest tradeoffs

1. **No tool_use blocks.** The CLI doesn't know about PILK's tool
   registry (that's what MCP would fix later). We strip ``tools``
   from the input and always return a text-only response. If the
   model genuinely needs a tool, it'll say so in text — the
   orchestrator can retry on the API path when that happens. V1
   just logs a warning and surfaces the text reply.
2. **Cold-start latency.** Each call spawns a fresh ``claude``
   subprocess (~1-3s). Noticeable but tolerable for chat. Warm-pool
   follow-up if it gets in the way.
3. **Stateless from CLI's side.** We pass ``--bare`` +
   ``--no-session-persistence``, so the CLI doesn't keep history.
   We flatten PILK's message history into a single prompt per
   turn. Cost-wise this is fine — the subscription doesn't charge
   per-token anyway.
4. **Model selection is overridden by the CLI.** The caller's
   ``model=`` is honoured when the CLI accepts ``--model``; older
   binaries ignore it. Subscription usually routes to Sonnet by
   default — that's fine for LIGHT.

### Boot-time discovery

Provider init resolves the ``claude`` binary on PATH (or the
configured override). If it's missing, init raises and
``build_providers`` skips this provider entirely — the governor
falls back to the Anthropic API provider for the LIGHT tier,
logging the fallback once.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from core.governor.providers.base import (
    PlannerResponse,
    TextBlock,
    ToolUseBlock,
    UsageLike,
)
from core.logging import get_logger

log = get_logger("pilkd.provider.claude_code")

DEFAULT_BINARY = "claude"
# Single-turn CLI runs: we don't want the model to plan its own
# multi-step loop — PILK's orchestrator owns that.
DEFAULT_MAX_TURNS = 1
# The CLI's JSON output mode emits one object per run; we only need
# a modest timeout for chat-scale replies. A runaway CLI gets killed.
DEFAULT_TIMEOUT_S = 60
# Cap how much conversation history we flatten into a single prompt.
# The subscription doesn't charge per token, but shipping a 200K-
# token history through argv is just slow.
HISTORY_CHAR_BUDGET = 40_000

# The CLI has a per-session brief PILK injects so every subscription-
# backed chat turn knows it's running under the daemon. Short on
# purpose; operator-facing system prompt still drives behaviour.
PILK_APPEND_PROMPT = (
    "You are answering a chat turn on behalf of PILK, the local "
    "execution OS. Keep replies short, voice-friendly, no markdown "
    "headings. If a task requires tools you do not have here, say so "
    "in plain text — do not fabricate a tool call. PILK's orchestrator "
    "will retry on the API path when it needs structured tool use."
)


class ClaudeCodeBinaryMissingError(RuntimeError):
    """Raised at provider init when the `claude` binary isn't on PATH
    and can't be resolved. Signals the caller to skip registering this
    provider so the governor fails over cleanly."""


class ClaudeCodeChatProvider:
    """Shells out to ``claude`` CLI for planner turns. Subscription-
    backed → $0 per call for operators on Max / Pro."""

    name = "claude_code"

    def __init__(
        self,
        *,
        binary: str | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        resolved = self._resolve_binary(binary or DEFAULT_BINARY)
        if resolved is None:
            raise ClaudeCodeBinaryMissingError(
                f"`{binary or DEFAULT_BINARY}` not on PATH. The "
                "subscription-backed chat provider won't register; "
                "install Claude Code CLI or set PILK_CLAUDE_CODE_BINARY."
            )
        self._binary = resolved
        self._max_turns = max(1, int(max_turns))
        self._timeout_s = max(5, int(timeout_s))

    @staticmethod
    def _resolve_binary(candidate: str) -> str | None:
        """Accept either a bare command name (resolved via PATH) or
        an absolute path. Return the absolute path on success, None
        when the binary can't be found."""
        if os.path.isabs(candidate):
            return candidate if Path(candidate).is_file() else None
        return shutil.which(candidate)

    # ── interface ──────────────────────────────────────────────

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
        del max_tokens, use_thinking, cache_control  # irrelevant to CLI
        prompt = self._flatten(messages)
        if tools:
            # We can't honour custom tools from the CLI. Let the
            # model know so it returns text rather than hallucinating
            # a tool call we can't execute.
            log.info(
                "claude_code_chat_stripped_tools",
                tool_count=len(tools),
            )

        argv = self._build_argv(system=system, user_prompt=prompt, model=model)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise RuntimeError(
                f"claude_code provider subprocess spawn failed: {e}"
            ) from e

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_s,
            )
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"claude_code provider timed out after {self._timeout_s}s"
            ) from e

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude_code provider exit={proc.returncode}: "
                f"{(stderr or stdout)[:200]}"
            )

        text, usage_model = self._parse_output(stdout)
        effective_model = usage_model or model or "claude-subscription"
        return PlannerResponse(
            content=[TextBlock(text=text)] if text else [],
            stop_reason="end_turn",
            usage=UsageLike(
                input_tokens=0,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            model=effective_model,
        )

    # ── helpers ────────────────────────────────────────────────

    def _build_argv(
        self, *, system: str, user_prompt: str, model: str,
    ) -> list[str]:
        argv: list[str] = [
            self._binary,
            "-p",
            "--output-format",
            "json",
            "--max-turns",
            str(self._max_turns),
            # Strictest permission mode — CLI shouldn't invoke any
            # tools on our behalf. If it tries, it gets blocked; we
            # want pure text generation here.
            "--permission-mode",
            "default",
            "--bare",
            "--no-session-persistence",
        ]
        if system:
            argv.extend([
                "--append-system-prompt",
                self._compose_system(system),
            ])
        if model:
            argv.extend(["--model", model])
        argv.append(user_prompt)
        return argv

    @staticmethod
    def _compose_system(system: str) -> str:
        """Layer PILK's orchestrator-side system prompt on top of the
        subscription-chat preamble. The preamble is tiny + fixed so
        the cache hits on the CLI side; the operator-facing prompt
        rides as the append-system-prompt string."""
        head = PILK_APPEND_PROMPT.strip()
        body = (system or "").strip()
        if not body:
            return head
        return f"{head}\n\n{body}"

    @staticmethod
    def _flatten(messages: list[dict[str, Any]]) -> str:
        """Flatten the orchestrator's message history into a single
        prompt. Roles are prefixed so the model keeps turn-taking
        context; the most recent turn lands at the bottom where the
        CLI expects the actual user question to be."""
        if not messages:
            return ""
        parts: list[str] = []
        for m in messages[-12:]:  # keep the most recent 12 exchanges
            role = str(m.get("role") or "user")
            content = m.get("content")
            text = ClaudeCodeChatProvider._message_text(content)
            if not text.strip():
                continue
            parts.append(f"{role}: {text}")
        joined = "\n\n".join(parts)
        if len(joined) > HISTORY_CHAR_BUDGET:
            # Drop the oldest entries until we're under the cap.
            # Keeping the *latest* exchange is what matters.
            while parts and len("\n\n".join(parts)) > HISTORY_CHAR_BUDGET:
                parts.pop(0)
            joined = "\n\n".join(parts)
        return joined

    @staticmethod
    def _message_text(content: Any) -> str:
        """Collapse the {role, content} dict's content into a string.
        Accepts either a raw string or a list of content blocks
        (Anthropic-shaped), pulling out text parts only."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text") or ""
                    if isinstance(text, str):
                        chunks.append(text)
            return "\n".join(chunks)
        return ""

    @staticmethod
    def _parse_output(stdout: str) -> tuple[str, str | None]:
        """Pull the final assistant text out of the CLI's JSON
        envelope. Tolerant of older/newer schemas and non-JSON stdout
        (treats as plain text)."""
        if not stdout:
            return "", None
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return stdout.strip(), None
        if not isinstance(payload, dict):
            return stdout.strip(), None
        result = payload.get("result")
        if isinstance(result, str):
            return result.strip(), payload.get("model") or None
        # Some CLI versions put text under message.content.
        msg = payload.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                return content.strip(), payload.get("model") or None
            if isinstance(content, list):
                chunks: list[str] = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        t = b.get("text") or ""
                        if isinstance(t, str):
                            chunks.append(t)
                if chunks:
                    return "\n".join(chunks).strip(), payload.get("model") or None
        return "", payload.get("model") or None


# Re-export unused symbols so tests / callers can satisfy static-
# analysis imports the same way other providers expose them.
__all__ = [
    "ClaudeCodeBinaryMissingError",
    "ClaudeCodeChatProvider",
    "TextBlock",
    "ToolUseBlock",
    "UsageLike",
]
