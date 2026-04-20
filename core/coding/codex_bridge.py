"""CodexBridge — delegate coding work to the local OpenAI `codex` CLI.

Mirror of :class:`ClaudeCodeBridge`, but driving the OpenAI Codex CLI
via ``codex exec``. Runs bill against the operator's ChatGPT
subscription when logged in via ``codex login`` (or an API key when
one is set in the environment), so picking this engine over
:class:`APIEngine` keeps coding tasks off PILK's per-token Anthropic
budget — the same "cheaper subscription" rationale that makes
Claude Code worth wiring.

Invocation we settle on:

    codex exec
          --full-auto                 # workspace-write + on-request approvals
          [--model <model>]
          [--cd <repo_path>]
          [--ephemeral]               # don't persist sessions to disk
          [--output-last-message <f>] # final agent text → tempfile
          <goal>

`--full-auto` is the default permission posture because PILK already
held a single higher-level approval for the delegated task; full
``--yolo`` (``--dangerously-bypass-approvals-and-sandbox``) is opt-in
via settings for trusted local runs.

Availability gate (cheap, never throws):
- ``codex`` binary resolvable (PATH or explicit path)
- ``codex --version`` exits 0 within 2s

Cloud (Railway) doesn't ship the CLI, so this stays unavailable there
and the router falls through to APIEngine.

Reference: https://developers.openai.com/codex/cli/reference
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from core.coding.base import CodeRunResult, CodeTask, EngineHealth
from core.logging import get_logger

log = get_logger("pilkd.coding.codex")

DEFAULT_BINARY = "codex"
PROBE_TIMEOUT_S = 2.0
RUN_TIMEOUT_S = 600.0  # 10 minutes — matches Claude Code's cap

# Known Codex sandbox / auto modes. The CLI accepts more; these are
# the ones PILK knows how to defend against in comments and tests.
VALID_SANDBOX_MODES = {
    "read-only",
    "workspace-write",
    "danger-full-access",
}


class CodexBridge:
    name = "codex"
    label = "OpenAI Codex (local)"

    def __init__(
        self,
        binary: str | None = None,
        *,
        model: str | None = None,
        sandbox_mode: str | None = None,
        full_auto: bool = True,
        yolo: bool = False,
        ephemeral: bool = True,
    ) -> None:
        self._binary = (binary or DEFAULT_BINARY).strip() or DEFAULT_BINARY
        self._model = model
        self._sandbox_mode = (
            sandbox_mode if sandbox_mode in VALID_SANDBOX_MODES else None
        )
        self._full_auto = full_auto
        self._yolo = yolo
        self._ephemeral = ephemeral
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
                    f"'{self._binary}' not on PATH — install OpenAI "
                    "Codex CLI (`npm i -g @openai/codex` or Homebrew) "
                    "and `codex login` to enable"
                ),
            )
        if not await self._probe():
            return EngineHealth(
                name=self.name,
                label=self.label,
                available=False,
                detail=f"{resolved} not responding to --version",
            )
        mode_bits: list[str] = [f"binary: {resolved}"]
        if self._yolo:
            mode_bits.append("mode: YOLO (bypass)")
        elif self._sandbox_mode:
            mode_bits.append(f"sandbox: {self._sandbox_mode}")
        elif self._full_auto:
            mode_bits.append("mode: full-auto")
        if self._model:
            mode_bits.append(f"model: {self._model}")
        return EngineHealth(
            name=self.name,
            label=self.label,
            available=True,
            detail=" · ".join(mode_bits),
        )

    async def run(self, task: CodeTask) -> CodeRunResult:
        resolved = self._resolve()
        if resolved is None:
            return CodeRunResult(
                engine=self.name,
                ok=False,
                summary=(
                    "Codex CLI not found — install `@openai/codex` "
                    "locally or pick a different engine."
                ),
            )
        cwd = self._cwd_for(task)
        # --output-last-message <file> writes just the final agent text
        # to a file. Simpler than parsing streaming --json; still gets
        # us a clean "result" payload without scraping stdout.
        output_file: Path | None = None
        tempdir = tempfile.mkdtemp(prefix="pilk-codex-")
        try:
            output_file = Path(tempdir) / "last_message.txt"
            argv = self._build_argv(resolved, task, output_file)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as e:
                log.exception("codex_spawn_failed")
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
                        f"Codex run timed out after {RUN_TIMEOUT_S:.0f}s."
                    ),
                )

            stdout = stdout_b.decode("utf-8", errors="replace").strip()
            stderr = stderr_b.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                return CodeRunResult(
                    engine=self.name,
                    ok=False,
                    summary=(
                        f"Codex exited {proc.returncode}: "
                        f"{(stderr or stdout)[:160]}"
                    ),
                    detail=stderr or stdout,
                    metadata={
                        "returncode": proc.returncode,
                        "cwd": str(cwd),
                        "argv": argv,
                    },
                )

            # Prefer the last-message file; fall back to stdout if the
            # flag wasn't supported or the file wasn't written.
            final_text = ""
            if output_file.exists():
                try:
                    final_text = output_file.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                except OSError:
                    final_text = ""
            if not final_text:
                final_text = stdout

            summary_line = (
                final_text.splitlines()[0] if final_text else "(no output)"
            )
            return CodeRunResult(
                engine=self.name,
                ok=True,
                summary=f"Codex ran: {summary_line[:120]}",
                detail=final_text,
                usd=0.0,  # subscription-billed; API-key runs aren't
                          # attributed here because pricing per model
                          # isn't in the CLI output. Ledger attribution
                          # is a follow-up.
                metadata={
                    "binary": resolved,
                    "cwd": str(cwd),
                    "scope": task.scope,
                    "argv": argv[1:],
                },
            )
        finally:
            # tempfile.mkdtemp() doesn't auto-clean. Best-effort removal.
            try:
                if output_file is not None and output_file.exists():
                    output_file.unlink()
                os.rmdir(tempdir)
            except OSError:
                pass

    def _build_argv(
        self,
        resolved: str,
        task: CodeTask,
        output_file: Path,
    ) -> list[str]:
        argv: list[str] = [resolved, "exec"]
        # Permission / sandbox posture. YOLO wins if set; then explicit
        # sandbox_mode; otherwise --full-auto as the default PILK
        # delegated-run posture.
        if self._yolo:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        elif self._sandbox_mode:
            argv.extend(["--sandbox", self._sandbox_mode])
        elif self._full_auto:
            argv.append("--full-auto")
        if self._model:
            argv.extend(["--model", self._model])
        if self._ephemeral:
            argv.append("--ephemeral")
        # `--cd` lets Codex operate in the requested repo. Without it
        # the CLI falls back to the subprocess's cwd (which we also
        # set below for belt-and-braces).
        if task.repo_path is not None:
            argv.extend(["--cd", str(task.repo_path)])
        argv.extend(["--output-last-message", str(output_file)])
        argv.append(task.goal)
        return argv

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
        return Path.cwd()
