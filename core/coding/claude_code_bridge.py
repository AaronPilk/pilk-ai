"""ClaudeCodeBridge — delegate coding work to the local `claude` CLI.

Runs the Claude Code CLI as a subprocess in ``claude --print <goal>``
mode. The CLI respects the operator's own login session (keyring-
stored refresh token or ``ANTHROPIC_API_KEY``), so runs are billed
against their Claude subscription rather than PILK's per-token API
spend — which is the whole point of picking this engine over the
API engine when a coding task would otherwise burn through tokens.

Responsibility model (from the original scaffold comment, unchanged):
- PILK holds a single approval covering the delegated task ("delegate
  coding task to Claude Code: <goal>"). Claude Code's own permission
  prompts handle fine-grained approvals inside the run.
- Cost: the engine returns ``usd=0.0`` so the Anthropic API daily cap
  is not polluted by Claude-Code-billed spend.

Availability gate (cheap, never throws):
- ``claude`` binary must be on PATH (or at ``PILK_CLAUDE_CODE_BINARY``)
- ``claude --version`` must exit 0 within 2s
Both are cached per-instance; a redeploy re-probes.

Cloud (Railway) does NOT ship the CLI, so this engine reports
unavailable there and the router falls back to the Agent SDK / API
engines.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from core.coding.base import CodeRunResult, CodeTask, EngineHealth
from core.logging import get_logger

log = get_logger("pilkd.coding.claude_code")

DEFAULT_BINARY = "claude"
PROBE_TIMEOUT_S = 2.0
RUN_TIMEOUT_S = 600.0  # 10 minutes — repo-scope refactors can be slow


class ClaudeCodeBridge:
    name = "claude-code"
    label = "Claude Code (local)"

    def __init__(self, binary: str | None = None) -> None:
        # `binary` accepts either a bare command name to resolve on
        # PATH or an absolute path. The legacy setting
        # `PILK_CLAUDE_CODE_BRIDGE_URL` is accepted for back-compat —
        # whatever it was set to we treat as the binary locator.
        self._binary = (binary or DEFAULT_BINARY).strip() or DEFAULT_BINARY
        self._resolved: str | None = None

    def _resolve(self) -> str | None:
        if self._resolved is not None:
            return self._resolved
        path = self._binary
        if os.path.isabs(path) and os.path.isfile(path):
            self._resolved = path
            return path
        found = shutil.which(path)
        if found is not None:
            self._resolved = found
        return self._resolved

    async def available(self) -> bool:
        if self._resolve() is None:
            return False
        return await self._probe()

    async def health(self) -> EngineHealth:
        resolved = self._resolve()
        if resolved is None:
            return EngineHealth(
                name=self.name,
                label=self.label,
                available=False,
                detail=(
                    f"'{self._binary}' not on PATH — install Claude Code "
                    "(https://docs.claude.com/code) to enable"
                ),
            )
        if not await self._probe():
            return EngineHealth(
                name=self.name,
                label=self.label,
                available=False,
                detail=f"{resolved} not responding to --version",
            )
        return EngineHealth(
            name=self.name,
            label=self.label,
            available=True,
            detail=f"binary: {resolved}",
        )

    async def run(self, task: CodeTask) -> CodeRunResult:
        resolved = self._resolve()
        if resolved is None:
            return CodeRunResult(
                engine=self.name,
                ok=False,
                summary=(
                    "Claude Code CLI not found — install it locally "
                    "or pick a different engine."
                ),
            )
        cwd = self._cwd_for(task)
        try:
            proc = await asyncio.create_subprocess_exec(
                resolved,
                "--print",
                "--output-format",
                "text",
                task.goal,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            log.exception("claude_code_spawn_failed")
            return CodeRunResult(
                engine=self.name,
                ok=False,
                summary=f"failed to start {resolved}: {e}",
            )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=RUN_TIMEOUT_S
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return CodeRunResult(
                engine=self.name,
                ok=False,
                summary=(
                    f"Claude Code run timed out after {RUN_TIMEOUT_S:.0f}s."
                ),
            )

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            return CodeRunResult(
                engine=self.name,
                ok=False,
                summary=(
                    f"Claude Code exited {proc.returncode}: "
                    f"{(stderr or stdout)[:160]}"
                ),
                detail=stderr or stdout,
                metadata={"returncode": proc.returncode, "cwd": str(cwd)},
            )

        summary_line = stdout.splitlines()[0] if stdout else "(no output)"
        return CodeRunResult(
            engine=self.name,
            ok=True,
            summary=f"Claude Code ran: {summary_line[:120]}",
            detail=stdout,
            usd=0.0,  # subscription-billed, stays out of the API ledger
            metadata={
                "binary": resolved,
                "cwd": str(cwd),
                "scope": task.scope,
            },
        )

    async def _probe(self) -> bool:
        resolved = self._resolve()
        if resolved is None:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                resolved,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            return False
        try:
            await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return False
        return proc.returncode == 0

    @staticmethod
    def _cwd_for(task: CodeTask) -> Path:
        if task.repo_path is not None:
            return task.repo_path.expanduser().resolve()
        # Repo-scope + unspecified path: let Claude Code open its own
        # default directory. For function-scope drafts, use a temp-ish
        # cwd so the CLI doesn't accidentally scan an unrelated repo.
        return Path.cwd()
