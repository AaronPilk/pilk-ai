"""ClaudeCodeBridge — delegate coding work to the local `claude` CLI.

Runs the Claude Code CLI as a subprocess in ``claude -p`` mode with the
flags that actually matter for a scripted PILK-driven invocation:

* ``--print`` + ``--output-format json`` — no TTY, structured output
  we can parse for cost, session id, and the final result string.
* ``--max-turns`` — bounded agent loop so a runaway refactor can't
  sit forever.
* ``--permission-mode`` — defaults to ``bypassPermissions`` because
  PILK has already secured a single approval for the delegated run;
  fine-grained per-tool prompts would stall in a headless context.
* ``--append-system-prompt`` — injects "you are driven by PILK"
  context so the CLI knows this is an automated run and doesn't try
  to emit interactive questions.
* ``--add-dir`` — when the task has a ``repo_path`` we add it as an
  extra working directory so Claude Code can read/write beyond its
  default cwd.
* ``--bare`` + ``--no-session-persistence`` — skip CLAUDE.md / hook /
  plugin / MCP auto-discovery and don't leave a resumable session
  behind. Faster starts, cleaner workspace.
* ``--max-budget-usd`` — optional hard spend cap (subscription runs
  still get a ceiling) pulled from the caller's ``max_budget_usd``.

All runs respect the operator's own Claude login (keyring-stored
refresh token or ``ANTHROPIC_API_KEY``), so they bill against the
Claude subscription rather than PILK's per-token API budget —
which is the whole reason this engine exists in the router.

Availability is cheap + cached per-instance:
- ``claude`` binary must be on PATH (or at ``PILK_CLAUDE_CODE_BINARY``)
- ``claude --version`` must exit 0 within 2s

Cloud (Railway) does NOT ship the CLI, so this engine reports
unavailable there and the router falls back to the Agent SDK / API
engines.

Reference: https://code.claude.com/docs/en/cli-reference
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from core.coding.base import CodeRunResult, CodeTask, EngineHealth
from core.logging import get_logger

log = get_logger("pilkd.coding.claude_code")

DEFAULT_BINARY = "claude"
DEFAULT_MAX_TURNS = 10
DEFAULT_PERMISSION_MODE = "bypassPermissions"
PROBE_TIMEOUT_S = 2.0
RUN_TIMEOUT_S = 600.0  # 10 minutes — repo-scope refactors can be slow

# Every run gets this prefix appended to the default system prompt so
# the CLI knows it's being driven by PILK rather than a human at a
# terminal. Keeps Claude Code from asking follow-up questions it has
# no way to receive in a non-interactive process.
PILK_APPEND_PROMPT = (
    "You are being driven non-interactively by PILK (Personal "
    "Intelligence Large-Language Kit). Complete the task directly; "
    "do not ask follow-up questions because no human is reading the "
    "stream mid-run. If a choice is ambiguous, pick the most "
    "conservative option and note the assumption in your final "
    "message."
)


class ClaudeCodeBridge:
    name = "claude-code"
    label = "Claude Code (local)"

    def __init__(
        self,
        binary: str | None = None,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        max_budget_usd: float | None = None,
        model: str | None = None,
    ) -> None:
        # `binary` accepts either a bare command name to resolve on
        # PATH or an absolute path. The legacy setting
        # `PILK_CLAUDE_CODE_BRIDGE_URL` is accepted for back-compat —
        # whatever it was set to we treat as the binary locator.
        self._binary = (binary or DEFAULT_BINARY).strip() or DEFAULT_BINARY
        self._max_turns = max(1, int(max_turns))
        self._permission_mode = permission_mode
        self._max_budget_usd = max_budget_usd
        self._model = model
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
        detail_bits = [f"binary: {resolved}", f"max_turns: {self._max_turns}"]
        if self._model:
            detail_bits.append(f"model: {self._model}")
        return EngineHealth(
            name=self.name,
            label=self.label,
            available=True,
            detail=" · ".join(detail_bits),
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
        # uvloop's UVProcess._init throws NotADirectoryError before
        # the subprocess ever starts if ``cwd`` doesn't resolve to an
        # existing directory. Catch it here with a friendly fallback
        # rather than letting it bubble as an opaque OSError.
        if not cwd.is_dir():
            log.warning(
                "claude_code_cwd_invalid",
                requested_cwd=str(cwd),
                fallback=str(Path.cwd()),
                repo_path=(
                    str(task.repo_path) if task.repo_path else None
                ),
            )
            cwd = Path.cwd()
        argv = self._build_argv(resolved, task)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            log.exception(
                "claude_code_spawn_failed",
                resolved=resolved,
                cwd=str(cwd),
                argv0=argv[0] if argv else None,
            )
            return CodeRunResult(
                engine=self.name,
                ok=False,
                summary=(
                    f"failed to start {resolved} from cwd {cwd}: "
                    f"{type(e).__name__}: {e}"
                ),
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
                metadata={
                    "returncode": proc.returncode,
                    "cwd": str(cwd),
                    "argv": argv,
                },
            )

        return self._parse_output(stdout, cwd, argv, task)

    def _build_argv(self, resolved: str, task: CodeTask) -> list[str]:
        argv: list[str] = [
            resolved,
            "-p",
            "--output-format",
            "json",
            "--max-turns",
            str(self._max_turns),
            "--permission-mode",
            self._permission_mode,
            "--append-system-prompt",
            PILK_APPEND_PROMPT,
            # `--bare` skips CLAUDE.md / hook / plugin / MCP / auto-
            # memory discovery so scripted calls start faster. We want
            # predictable, low-latency runs from pilkd; the operator's
            # per-project tweaks don't belong in a PILK-delegated run.
            "--bare",
            # `--no-session-persistence` keeps scripted runs from
            # leaving resumable sessions behind on disk — PILK is the
            # orchestrator and tracks runs in its own plan store.
            "--no-session-persistence",
        ]
        if self._model:
            argv.extend(["--model", self._model])
        if self._max_budget_usd is not None and self._max_budget_usd > 0:
            argv.extend(["--max-budget-usd", str(self._max_budget_usd)])
        if task.repo_path is not None:
            argv.extend(["--add-dir", str(task.repo_path)])
        argv.append(task.goal)
        return argv

    def _parse_output(
        self,
        stdout: str,
        cwd: Path,
        argv: list[str],
        task: CodeTask,
    ) -> CodeRunResult:
        """Parse the JSON result envelope the CLI prints under
        ``--output-format json``. Schema we care about:
            {
              "type": "result",
              "subtype": "success"|"error_during_execution"|...,
              "is_error": bool,
              "result": "<final assistant text>",
              "total_cost_usd": float,  // subscription runs report 0
              "session_id": "...",
              "num_turns": int
            }
        Older CLI versions may return plain text — fall back to
        treating stdout as the result text.
        """
        metadata: dict = {
            "binary": argv[0],
            "cwd": str(cwd),
            "scope": task.scope,
            "argv": argv[1:],  # keep stdout's binary path out of logs
        }
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            # Non-JSON (older CLI, or --bare mode without json output
            # for some reason). Treat the whole stdout as the result.
            summary_line = stdout.splitlines()[0] if stdout else "(no output)"
            return CodeRunResult(
                engine=self.name,
                ok=True,
                summary=f"Claude Code ran: {summary_line[:120]}",
                detail=stdout,
                usd=0.0,
                metadata={**metadata, "output_format": "raw"},
            )

        if isinstance(payload, dict):
            result_text = str(payload.get("result") or "").strip()
            is_error = bool(payload.get("is_error"))
            subtype = payload.get("subtype")
            metadata.update(
                {
                    "session_id": payload.get("session_id"),
                    "num_turns": payload.get("num_turns"),
                    "subtype": subtype,
                }
            )
            cost_usd = 0.0  # subscription-billed by default
            try:
                raw_cost = payload.get("total_cost_usd")
                if raw_cost is not None:
                    cost_usd = float(raw_cost)
            except (TypeError, ValueError):
                pass
            if is_error:
                return CodeRunResult(
                    engine=self.name,
                    ok=False,
                    summary=(
                        f"Claude Code failed ({subtype or 'error'}): "
                        f"{result_text[:160] or '(no detail)'}"
                    ),
                    detail=result_text or stdout,
                    usd=cost_usd,
                    metadata=metadata,
                )
            summary_line = (
                result_text.splitlines()[0] if result_text else "(no output)"
            )
            return CodeRunResult(
                engine=self.name,
                ok=True,
                summary=f"Claude Code ran: {summary_line[:120]}",
                detail=result_text,
                # Subscription runs: 0. API-key runs: real cost. Keep
                # whichever the CLI reports — the ledger decides how to
                # attribute it downstream.
                usd=cost_usd,
                metadata=metadata,
            )

        # Unexpected JSON shape — hand the raw stdout back so nothing
        # is lost, but flag the mismatch in metadata.
        metadata["output_format"] = "unexpected_json"
        return CodeRunResult(
            engine=self.name,
            ok=True,
            summary="Claude Code ran (unrecognised JSON envelope).",
            detail=stdout,
            usd=0.0,
            metadata=metadata,
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
