"""Workspace-scoped shell execution.

Commands run with `cwd` set to `~/PILK/workspace/` and inherit a sanitized
env (PATH + HOME only). Output is captured, truncated, and returned as a
single structured blob. Hard-killed after `PILK_SHELL_TIMEOUT_S`.
"""

from __future__ import annotations

import asyncio
import os
import shlex

from core.config import get_settings
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

MAX_OUTPUT_BYTES = 32 * 1024


def _workspace_cwd() -> str:
    root = get_settings().workspace_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _sanitized_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
    }


async def _shell_exec(args: dict, _ctx: ToolContext) -> ToolOutcome:
    command = str(args["command"])
    settings = get_settings()
    timeout = int(args.get("timeout_s") or settings.shell_timeout_s)

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=_workspace_cwd(),
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

    out = stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES]
    err = stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES]
    rc = proc.returncode if proc.returncode is not None else -1
    body = f"$ {command}\n[return_code={rc}]\n--- stdout ---\n{out}"
    if err:
        body += f"\n--- stderr ---\n{err}"
    return ToolOutcome(
        content=body,
        is_error=rc != 0,
        data={"return_code": rc, "stdout": out, "stderr": err},
    )


shell_exec_tool = Tool(
    name="shell_exec",
    description=(
        "Run a shell command inside the PILK workspace. Working directory is "
        "fixed to ~/PILK/workspace/; the environment is sanitized (PATH, HOME, "
        "LANG only). Hard-killed after the timeout. Returns return_code plus "
        "truncated stdout/stderr."
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
        },
        "required": ["command"],
    },
    risk=RiskClass.EXEC_LOCAL,
    handler=_shell_exec,
)
