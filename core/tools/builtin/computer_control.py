"""IRREVERSIBLE computer_* tools — let PILK reach outside the
workspace sandbox onto the real machine.

Four tools cover the V1 surface:

    computer_fs_read       read any readable file under $HOME
    computer_fs_write      write any writeable file under $HOME
    computer_shell         run an unscoped shell command
    computer_osascript     run AppleScript (macOS only)

Every one of these is ``IRREVERSIBLE`` risk class — the strictest
gate in the policy matrix. Three extra guardrails layer on top of
the base approval flow, all implemented via
``core.policy.computer_control.ComputerControlGate``:

1. **Enable toggle.** Each tool refuses to even consider the call
   until ``computer_control_enabled`` is ``"true"`` in Settings.
2. **Per-call confirmation token.** First call returns a token +
   preview; second call with the token runs for real. Tokens are
   single-use, 5-minute TTL, bound to the exact tool + args, so
   the agent can't splice one across different payloads.
3. **Daily-limit + audit log.** Every verified call bumps a UTC-
   day counter (default 20/day) and appends a line to
   ``~/PILK/logs/computer-control.jsonl`` so Sentinel can tail-watch.

Hard-blocked paths (``~/.ssh``, ``~/.aws``, ``/etc``, keychain, …)
are refused regardless of enable state or token. Not configurable.

The tools are registered on the global tool registry unconditionally
at boot so the operator can see them on agent detail panels, but
they return a clean "disabled" error until Settings flips the
switch.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shlex
from pathlib import Path
from typing import Any

from core.logging import get_logger
from core.policy.computer_control import (
    BlockedPathError,
    ComputerControlDisabledError,
    ComputerControlGate,
    DailyLimitExceededError,
    TokenRequiredError,
    fresh_audit_entry,
)
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.tools.computer_control")

# Caps so an accidentally huge read / write doesn't lock up the
# daemon. Operators who need more hit the limit, bump it, move on.
FS_READ_MAX_BYTES = 512 * 1024           # 512 KiB per call
FS_WRITE_MAX_BYTES = 2 * 1024 * 1024     # 2 MiB per call
SHELL_TIMEOUT_S_DEFAULT = 30
SHELL_TIMEOUT_S_MAX = 300
OSASCRIPT_TIMEOUT_S_DEFAULT = 30


def _resolve_home_path(raw: str) -> Path:
    """Expand ~ + resolve symlinks. Relative paths are rejected —
    an ambiguous relative path is exactly the kind of mistake
    IRREVERSIBLE tools can't recover from."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("path is required")
    if text.startswith("~"):
        text = os.path.expanduser(text)
    p = Path(text)
    if not p.is_absolute():
        raise ValueError(
            f"path must be absolute (no relative paths for IRREVERSIBLE "
            f"tools): {raw}"
        )
    return p.expanduser().resolve()


def _args_summary(args: dict[str, Any], limit: int = 240) -> str:
    """Short, log-safe description of args for the audit trail. We
    redact obviously long payloads (a shell command with embedded
    credentials, a big file write) to keep the log readable."""
    interesting = {
        k: v for k, v in (args or {}).items() if k != "confirmation_token"
    }
    s = str(interesting)
    if len(s) > limit:
        s = s[: limit - 3] + "..."
    return s


_SINGLETON_GATE: ComputerControlGate | None = None


def set_gate(gate: ComputerControlGate) -> None:
    """Called once at app startup so every computer_* tool sees the
    same shared gate (same rate-limit counter, same audit log, same
    in-memory token store). Tests reset this between cases."""
    global _SINGLETON_GATE
    _SINGLETON_GATE = gate


def reset_gate_for_tests() -> None:
    """Only use from tests."""
    global _SINGLETON_GATE
    _SINGLETON_GATE = None


def _get_gate() -> ComputerControlGate:
    """Resolve the shared gate. If the app never registered one
    (e.g. in a unit test that skipped startup), build a lazy
    singleton pointing at the PILK home."""
    global _SINGLETON_GATE
    if _SINGLETON_GATE is None:
        from core.config import get_settings
        from core.policy.computer_control import build_default_gate
        _SINGLETON_GATE = build_default_gate(get_settings().home)
    return _SINGLETON_GATE


def _preamble(
    tool_name: str, args: dict[str, Any], gate: ComputerControlGate,
) -> ToolOutcome | None:
    """Apply enable + token + daily-limit checks in that order.
    Returns a ToolOutcome to bail early, or None to proceed.

    On "token needed" we return a non-error ToolOutcome with the
    token so the agent sees "re-call me with confirmation_token=X"
    rather than a hard failure. That matches the real UX: the first
    call is supposed to ask for the token."""
    try:
        gate.require_enabled()
    except ComputerControlDisabledError as e:
        entry = fresh_audit_entry(
            tool_name, _args_summary(args), outcome="denied",
            detail="disabled",
        )
        gate.append_audit(entry)
        return ToolOutcome(content=str(e), is_error=True)

    token = str(args.get("confirmation_token") or "").strip() or None
    args_without_token = {
        k: v for k, v in args.items() if k != "confirmation_token"
    }
    if token is None:
        issued = gate.issue_token(tool_name, args_without_token)
        preview = _args_summary(args_without_token)
        return ToolOutcome(
            content=(
                f"About to run {tool_name} on:\n  {preview}\n\n"
                f"Re-call {tool_name} with the same args plus "
                f"`confirmation_token=\"{issued.token}\"` to execute. "
                f"Token expires in ~5 minutes and is single-use."
            ),
            data={
                "needs_confirmation": True,
                "confirmation_token": issued.token,
                "expires_at": issued.expires_at,
            },
        )
    try:
        gate.verify_and_consume_token(
            tool_name, args_without_token, token,
        )
    except TokenRequiredError as e:
        return ToolOutcome(
            content=(
                "Confirmation token invalid, mismatched, or expired. "
                f"A fresh token was issued — re-call with "
                f"confirmation_token=\"{e.token}\"."
            ),
            data={
                "needs_confirmation": True,
                "confirmation_token": e.token,
                "expires_at": e.expires_at,
            },
        )

    try:
        count = gate.check_and_bump_daily()
    except DailyLimitExceededError as e:
        entry = fresh_audit_entry(
            tool_name, _args_summary(args_without_token),
            outcome="denied", detail="daily_limit",
        )
        gate.append_audit(entry)
        return ToolOutcome(content=str(e), is_error=True)
    log.info(
        "computer_control_check_passed",
        tool=tool_name,
        daily_count=count,
    )
    return None


# ── computer_fs_read ────────────────────────────────────────────


async def _fs_read_handler(args: dict, ctx: ToolContext) -> ToolOutcome:
    gate = _get_gate()
    early = _preamble("computer_fs_read", args, gate)
    if early is not None:
        return early
    try:
        p = _resolve_home_path(str(args.get("path") or ""))
        gate.check_path(p)
    except ValueError as e:
        gate.append_audit(fresh_audit_entry(
            "computer_fs_read", _args_summary(args),
            outcome="error", detail=str(e),
        ))
        return ToolOutcome(content=str(e), is_error=True)
    except BlockedPathError as e:
        gate.append_audit(fresh_audit_entry(
            "computer_fs_read", _args_summary(args),
            outcome="denied", detail=str(e),
        ))
        return ToolOutcome(content=str(e), is_error=True)
    if not p.exists():
        gate.append_audit(fresh_audit_entry(
            "computer_fs_read", _args_summary(args),
            outcome="error", detail="not found",
        ))
        return ToolOutcome(content=f"not found: {p}", is_error=True)
    if not p.is_file():
        gate.append_audit(fresh_audit_entry(
            "computer_fs_read", _args_summary(args),
            outcome="error", detail="not a file",
        ))
        return ToolOutcome(content=f"not a file: {p}", is_error=True)
    try:
        raw = p.read_bytes()
    except OSError as e:
        gate.append_audit(fresh_audit_entry(
            "computer_fs_read", _args_summary(args),
            outcome="error", detail=str(e),
        ))
        return ToolOutcome(content=f"read error: {e}", is_error=True)
    truncated = len(raw) > FS_READ_MAX_BYTES
    body = raw[:FS_READ_MAX_BYTES].decode("utf-8", errors="replace")
    if truncated:
        body += (
            f"\n\n[truncated — {len(raw)} bytes total, "
            f"shown {FS_READ_MAX_BYTES}]"
        )
    gate.append_audit(fresh_audit_entry(
        "computer_fs_read", _args_summary(args), outcome="ok",
        detail=f"{len(raw)}B",
    ))
    return ToolOutcome(
        content=body,
        data={"path": str(p), "bytes": len(raw), "truncated": truncated},
    )


computer_fs_read_tool = Tool(
    name="computer_fs_read",
    description=(
        "Read any readable file under $HOME (bypasses the workspace "
        "sandbox). IRREVERSIBLE risk: requires computer_control_"
        "enabled in Settings + a per-call confirmation_token (first "
        "call returns one; re-call with it to execute). Reads are "
        "capped at 512 KiB; longer files are truncated with a "
        "marker. Hard-blocked paths (~/.ssh, ~/.aws, keychain, /etc) "
        "are refused regardless."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Absolute path. ~ is expanded. Relative paths are "
                    "rejected — IRREVERSIBLE tools don't disambiguate "
                    "implicit cwd."
                ),
            },
            "confirmation_token": {
                "type": "string",
                "description": (
                    "Token returned by the preceding call to this "
                    "tool with these exact args. Required to execute."
                ),
            },
        },
        "required": ["path"],
    },
    risk=RiskClass.IRREVERSIBLE,
    handler=_fs_read_handler,
)


# ── computer_fs_write ───────────────────────────────────────────


async def _fs_write_handler(args: dict, ctx: ToolContext) -> ToolOutcome:
    gate = _get_gate()
    early = _preamble("computer_fs_write", args, gate)
    if early is not None:
        return early
    content = args.get("content")
    if not isinstance(content, str):
        gate.append_audit(fresh_audit_entry(
            "computer_fs_write", _args_summary(args),
            outcome="error", detail="content must be string",
        ))
        return ToolOutcome(
            content="content must be a string", is_error=True,
        )
    data = content.encode("utf-8")
    if len(data) > FS_WRITE_MAX_BYTES:
        gate.append_audit(fresh_audit_entry(
            "computer_fs_write", _args_summary(args),
            outcome="error", detail=f"over {FS_WRITE_MAX_BYTES} bytes",
        ))
        return ToolOutcome(
            content=(
                f"content exceeds per-write cap "
                f"({len(data)} > {FS_WRITE_MAX_BYTES} bytes)"
            ),
            is_error=True,
        )
    try:
        p = _resolve_home_path(str(args.get("path") or ""))
        gate.check_path(p)
    except ValueError as e:
        gate.append_audit(fresh_audit_entry(
            "computer_fs_write", _args_summary(args),
            outcome="error", detail=str(e),
        ))
        return ToolOutcome(content=str(e), is_error=True)
    except BlockedPathError as e:
        gate.append_audit(fresh_audit_entry(
            "computer_fs_write", _args_summary(args),
            outcome="denied", detail=str(e),
        ))
        return ToolOutcome(content=str(e), is_error=True)
    append = bool(args.get("append") or False)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "ab" if append else "wb"
        with p.open(mode) as fh:
            fh.write(data)
    except OSError as e:
        gate.append_audit(fresh_audit_entry(
            "computer_fs_write", _args_summary(args),
            outcome="error", detail=str(e),
        ))
        return ToolOutcome(content=f"write error: {e}", is_error=True)
    gate.append_audit(fresh_audit_entry(
        "computer_fs_write", _args_summary(args), outcome="ok",
        detail=f"{len(data)}B append={append}",
    ))
    return ToolOutcome(
        content=f"Wrote {len(data)} bytes → {p} (append={append}).",
        data={"path": str(p), "bytes": len(data), "append": append},
    )


computer_fs_write_tool = Tool(
    name="computer_fs_write",
    description=(
        "Write to any writeable file under $HOME (bypasses the "
        "workspace sandbox). IRREVERSIBLE — needs enable + "
        "confirmation_token + approval. Max 2 MiB per call. Same "
        "hard-block list as computer_fs_read. Set `append: true` to "
        "extend an existing file rather than overwrite it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "append": {"type": "boolean"},
            "confirmation_token": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    risk=RiskClass.IRREVERSIBLE,
    handler=_fs_write_handler,
)


# ── computer_shell ──────────────────────────────────────────────


async def _shell_handler(args: dict, ctx: ToolContext) -> ToolOutcome:
    gate = _get_gate()
    early = _preamble("computer_shell", args, gate)
    if early is not None:
        return early
    command = str(args.get("command") or "").strip()
    if not command:
        gate.append_audit(fresh_audit_entry(
            "computer_shell", _args_summary(args),
            outcome="error", detail="missing command",
        ))
        return ToolOutcome(content="command is required", is_error=True)
    timeout = max(
        1, min(
            int(args.get("timeout_s") or SHELL_TIMEOUT_S_DEFAULT),
            SHELL_TIMEOUT_S_MAX,
        )
    )
    cwd_raw = str(args.get("cwd") or "").strip()
    cwd: Path | None = None
    if cwd_raw:
        try:
            cwd = _resolve_home_path(cwd_raw)
            gate.check_path(cwd)
        except (ValueError, BlockedPathError) as e:
            gate.append_audit(fresh_audit_entry(
                "computer_shell", _args_summary(args),
                outcome="denied" if isinstance(e, BlockedPathError) else "error",
                detail=str(e),
            ))
            return ToolOutcome(content=str(e), is_error=True)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except TimeoutError:
            with _suppress_process_errors():
                proc.kill()
                await proc.wait()
            gate.append_audit(fresh_audit_entry(
                "computer_shell", _args_summary(args),
                outcome="error", detail=f"timeout after {timeout}s",
            ))
            return ToolOutcome(
                content=f"shell timed out after {timeout}s",
                is_error=True,
            )
    except OSError as e:
        gate.append_audit(fresh_audit_entry(
            "computer_shell", _args_summary(args),
            outcome="error", detail=str(e),
        ))
        return ToolOutcome(
            content=f"shell exec error: {e}", is_error=True,
        )
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    exit_code = proc.returncode if proc.returncode is not None else -1
    outcome_kind = "ok" if exit_code == 0 else "error"
    gate.append_audit(fresh_audit_entry(
        "computer_shell", _args_summary(args), outcome=outcome_kind,
        detail=f"exit={exit_code}",
    ))
    preview = (
        (stdout[-2000:] if len(stdout) > 2000 else stdout)
        + ("\n\n[stderr]\n" + (stderr[-1000:] if stderr else "") if stderr else "")
    )
    return ToolOutcome(
        content=(
            f"Exit {exit_code}. "
            + (
                "Output:\n" + preview
                if stdout or stderr
                else "(no output)"
            )
        ),
        is_error=(exit_code != 0),
        data={
            "command": command,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "cwd": str(cwd) if cwd else None,
        },
    )


computer_shell_tool = Tool(
    name="computer_shell",
    description=(
        "Run a shell command on the operator's real machine. "
        "Bypasses the sandboxed workspace shell. IRREVERSIBLE — "
        "enable + confirmation_token + approval + daily-limit. "
        "Defaults to 30s timeout, max 300s. Output truncated at "
        "~2000 chars stdout / 1000 chars stderr in the chat view "
        "(full output still lands in `data`)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Full shell command. Runs via /bin/sh -c, so "
                    "pipes + redirects are honoured."
                ),
            },
            "cwd": {
                "type": "string",
                "description": "Absolute cwd for the command.",
            },
            "timeout_s": {
                "type": "integer",
                "minimum": 1,
                "maximum": SHELL_TIMEOUT_S_MAX,
            },
            "confirmation_token": {"type": "string"},
        },
        "required": ["command"],
    },
    risk=RiskClass.IRREVERSIBLE,
    handler=_shell_handler,
)


# ── computer_osascript (macOS) ──────────────────────────────────


async def _osascript_handler(args: dict, ctx: ToolContext) -> ToolOutcome:
    gate = _get_gate()
    early = _preamble("computer_osascript", args, gate)
    if early is not None:
        return early
    if platform.system() != "Darwin":
        gate.append_audit(fresh_audit_entry(
            "computer_osascript", _args_summary(args),
            outcome="error", detail=f"unsupported platform {platform.system()}",
        ))
        return ToolOutcome(
            content=(
                "computer_osascript only runs on macOS. Current "
                f"platform: {platform.system()}."
            ),
            is_error=True,
        )
    script = str(args.get("script") or "").strip()
    if not script:
        gate.append_audit(fresh_audit_entry(
            "computer_osascript", _args_summary(args),
            outcome="error", detail="missing script",
        ))
        return ToolOutcome(content="script is required", is_error=True)
    timeout = max(
        1, min(
            int(args.get("timeout_s") or OSASCRIPT_TIMEOUT_S_DEFAULT),
            SHELL_TIMEOUT_S_MAX,
        ),
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except TimeoutError:
            with _suppress_process_errors():
                proc.kill()
                await proc.wait()
            gate.append_audit(fresh_audit_entry(
                "computer_osascript", _args_summary(args),
                outcome="error", detail=f"timeout after {timeout}s",
            ))
            return ToolOutcome(
                content=f"osascript timed out after {timeout}s",
                is_error=True,
            )
    except FileNotFoundError:
        gate.append_audit(fresh_audit_entry(
            "computer_osascript", _args_summary(args),
            outcome="error", detail="osascript binary missing",
        ))
        return ToolOutcome(
            content=(
                "osascript binary not found. macOS usually ships it "
                "at /usr/bin/osascript — verify with "
                f"`which osascript`. shlex: {shlex.quote(script[:120])}"
            ),
            is_error=True,
        )
    except OSError as e:
        gate.append_audit(fresh_audit_entry(
            "computer_osascript", _args_summary(args),
            outcome="error", detail=str(e),
        ))
        return ToolOutcome(content=f"exec error: {e}", is_error=True)
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    exit_code = proc.returncode if proc.returncode is not None else -1
    outcome_kind = "ok" if exit_code == 0 else "error"
    gate.append_audit(fresh_audit_entry(
        "computer_osascript", _args_summary(args),
        outcome=outcome_kind, detail=f"exit={exit_code}",
    ))
    body = stdout if stdout else "(no output)"
    if stderr:
        body += "\n\n[stderr]\n" + stderr
    return ToolOutcome(
        content=f"Exit {exit_code}. {body}",
        is_error=(exit_code != 0),
        data={
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        },
    )


computer_osascript_tool = Tool(
    name="computer_osascript",
    description=(
        "Run AppleScript via `osascript -e <script>` (macOS only). "
        "Use to launch apps, control windows, scriptable keystrokes, "
        "or drive any scriptable application. IRREVERSIBLE — needs "
        "enable + confirmation_token + approval. Default timeout 30s; "
        "max 300s."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": (
                    "AppleScript source. Example: "
                    "'tell application \"Safari\" to activate'."
                ),
            },
            "timeout_s": {
                "type": "integer",
                "minimum": 1,
                "maximum": SHELL_TIMEOUT_S_MAX,
            },
            "confirmation_token": {"type": "string"},
        },
        "required": ["script"],
    },
    risk=RiskClass.IRREVERSIBLE,
    handler=_osascript_handler,
)


COMPUTER_CONTROL_TOOLS: list[Tool] = [
    computer_fs_read_tool,
    computer_fs_write_tool,
    computer_shell_tool,
    computer_osascript_tool,
]


def _suppress_process_errors():
    """Closing a dead process can raise — we don't care; the timeout
    already surfaced the real problem. Returns a context manager so
    call sites read the same as before."""
    return contextlib.suppress(ProcessLookupError, OSError)
