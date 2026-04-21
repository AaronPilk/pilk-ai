"""Workspace-scoped shell execution.

Commands run with `cwd` set to `~/PILK/workspace/` and inherit a sanitized
env (PATH + HOME only). Output is captured, truncated, and returned as a
single structured blob. Hard-killed after `PILK_SHELL_TIMEOUT_S`.

Output shaping: by default stdout/stderr are truncated at
``MAX_OUTPUT_BYTES``. For long logs the caller can pass ``head_lines``
and/or ``tail_lines`` to return only the first N / last M lines of
stdout — typical "I ran the build, show me the first few lines of
context and the last few lines of the failure" flow. Both can be set
together, in which case they're joined with a ``[… N lines elided …]``
marker so the model knows it saw the ends, not the middle.
"""

from __future__ import annotations

import asyncio
import os
import shlex

from core.config import get_settings
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

MAX_OUTPUT_BYTES = 32 * 1024
# Upper bound on head_lines / tail_lines. Bigger values get clamped.
# 2000 lines of a log is already more than a planner turn should
# reasonably reason about; the operator can always drop to the shell
# if they need the full dump.
MAX_SHAPED_LINES = 2000


def _shape_output(
    text: str, *, head_lines: int | None, tail_lines: int | None,
) -> str:
    """Apply head/tail line shaping to captured output.

    - Both None → return ``text`` unchanged.
    - Only head → first N lines.
    - Only tail → last N lines.
    - Both set  → first head_lines + a skip marker + last tail_lines,
      but only if the raw line count actually exceeds head+tail (so
      we don't pretend to truncate a short log).
    """
    if head_lines is None and tail_lines is None:
        return text
    lines = text.splitlines(keepends=True)
    total = len(lines)
    head_n = max(0, min(head_lines or 0, MAX_SHAPED_LINES))
    tail_n = max(0, min(tail_lines or 0, MAX_SHAPED_LINES))
    if head_n and tail_n:
        if head_n + tail_n >= total:
            return text
        head_part = "".join(lines[:head_n])
        tail_part = "".join(lines[-tail_n:])
        elided = total - head_n - tail_n
        # Preserve a trailing newline on the marker so the tail starts
        # on a fresh line whether or not the head ended on one.
        return (
            head_part
            + f"\n[… {elided} line(s) elided …]\n"
            + tail_part
        )
    if head_n:
        if head_n >= total:
            return text
        return "".join(lines[:head_n])
    # tail_n only
    if tail_n >= total:
        return text
    return "".join(lines[-tail_n:])


def _cwd_for(ctx: ToolContext) -> str:
    if ctx.sandbox_root is not None:
        root = ctx.sandbox_root.expanduser().resolve()
    else:
        root = get_settings().workspace_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _sanitized_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
    }


def _int_or_none(v) -> int | None:
    """Lenient int coerce — JSON numbers arrive as ints, but a caller
    might pass a stringified number. Anything else → None (ignored)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None  # guard against True coercing to 1 silently
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


async def _shell_exec(args: dict, ctx: ToolContext) -> ToolOutcome:
    command = str(args["command"])
    settings = get_settings()
    timeout = int(args.get("timeout_s") or settings.shell_timeout_s)
    head_lines = _int_or_none(args.get("head_lines"))
    tail_lines = _int_or_none(args.get("tail_lines"))

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=_cwd_for(ctx),
        env=_sanitized_env(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolOutcome(
            content=f"timed out after {timeout}s: {shlex.quote(command)}",
            is_error=True,
            data={"return_code": None, "timed_out": True},
        )

    raw_out = stdout.decode("utf-8", errors="replace")
    raw_err = stderr.decode("utf-8", errors="replace")
    # Line-shape first so head/tail sees the full output, then byte-
    # truncate as a last-resort safety net for pathological lines.
    shaped_out = _shape_output(
        raw_out, head_lines=head_lines, tail_lines=tail_lines,
    )
    out = shaped_out[:MAX_OUTPUT_BYTES]
    err = raw_err[:MAX_OUTPUT_BYTES]
    rc = proc.returncode if proc.returncode is not None else -1
    body = f"$ {command}\n[return_code={rc}]\n--- stdout ---\n{out}"
    if err:
        body += f"\n--- stderr ---\n{err}"
    return ToolOutcome(
        content=body,
        is_error=rc != 0,
        data={
            "return_code": rc,
            "stdout": out,
            "stderr": err,
            "stdout_lines_total": len(raw_out.splitlines()),
        },
    )


shell_exec_tool = Tool(
    name="shell_exec",
    description=(
        "Run a shell command inside the PILK workspace. Working directory is "
        "fixed to ~/PILK/workspace/; the environment is sanitized (PATH, HOME, "
        "LANG only). Hard-killed after the timeout. Returns return_code plus "
        "stdout/stderr (byte-truncated to ~32KB each). Use head_lines and/or "
        "tail_lines when you only need the top / bottom slice of a long log; "
        "both together trim the middle with a clear elision marker."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute (passed to /bin/sh -c).",
            },
            "timeout_s": {
                "type": "integer",
                "description": "Optional per-call timeout in seconds.",
                "minimum": 1,
                "maximum": 300,
            },
            "head_lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SHAPED_LINES,
                "description": (
                    "Return only the first N lines of stdout. Combine "
                    "with tail_lines to trim the middle."
                ),
            },
            "tail_lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SHAPED_LINES,
                "description": (
                    "Return only the last N lines of stdout. Typical "
                    "use: 'show me just the error trailer' on a long "
                    "build."
                ),
            },
        },
        "required": ["command"],
    },
    risk=RiskClass.EXEC_LOCAL,
    handler=_shell_exec,
)
