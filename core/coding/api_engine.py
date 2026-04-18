"""APIEngine — bare Anthropic Messages API.

Always-available fallback. Given a coding `goal`, asks Claude for a
single response (no tool loop, no filesystem writes) and returns the
text as the run's `detail`. A follow-up batch can extend this with a
proper fs_read/fs_write/shell_exec tool loop behind the approval gate;
for now it's a *draft* engine, not a *build* engine, which matches the
user-facing wording ("Run a coding task → drafted a response").

Writing to disk intentionally does NOT happen here. Anything that
modifies the repo must still go through the tool gateway with its
normal approval + sandbox semantics.
"""

from __future__ import annotations

import anthropic

from core.coding.base import CodeRunResult, CodeTask, EngineHealth
from core.logging import get_logger

log = get_logger("pilkd.coding.api")

SYSTEM_PROMPT = (
    "You are PILK's code-drafting engine. Given a coding task, return a "
    "concise response with the smallest helpful code block and a short "
    "explanation. Do not modify files — PILK routes real edits through "
    "its approval queue separately. Keep prose short and to the point."
)


class APIEngine:
    name = "api"
    label = "Anthropic API (draft)"

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None,
        model: str,
        max_tokens: int = 1024,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

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
            detail=f"model: {self._model}",
        )

    async def run(self, task: CodeTask) -> CodeRunResult:
        if self._client is None:
            return CodeRunResult(
                engine=self.name,
                ok=False,
                summary="API engine unavailable — no Anthropic API key set.",
            )
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _user_prompt(task)}],
            )
        except Exception as e:
            log.exception("api_engine_failed")
            return CodeRunResult(
                engine=self.name,
                ok=False,
                summary=f"API engine failed: {type(e).__name__}: {e}",
            )

        text = _join_text_blocks(resp.content)
        summary = text.splitlines()[0] if text else "(no response)"
        return CodeRunResult(
            engine=self.name,
            ok=True,
            summary=f"Drafted a response: {summary[:120]}",
            detail=text,
            usd=0.0,  # real USD is attributed by the ledger via provider usage
            metadata={
                "model": self._model,
                "scope": task.scope,
                "stop_reason": getattr(resp, "stop_reason", None),
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
