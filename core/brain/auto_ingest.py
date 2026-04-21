"""Auto-ingest on boot — seed the brain vault with the operator's
local Claude Code transcripts without waiting for a chat command.

The ``brain_ingest_claude_code`` tool exists, but nothing currently
calls it on startup; the operator has to say "ingest my Claude Code
history" in chat for anything to land. This module wires a small
background task into the daemon's lifespan so the vault fills itself
during the first boot after the feature arrives, and re-runs cheaply
on subsequent boots to pick up new project folders.

### Shape

- Spawned as an asyncio task during app startup. Does NOT block boot.
- Calls ``scan_projects`` against ``~/.claude/projects`` (or the
  operator-overridden root), writes one markdown note per project
  into the vault under ``ingested/claude-code/``.
- Idempotent — Vault.write overwrites at the derived path, so re-
  running overwrites with the latest state of the source.
- Logs a single ``brain_auto_ingest_started`` on entry and either
  ``brain_auto_ingest_completed`` (with counts) or
  ``brain_auto_ingest_failed`` (with the exception text) on exit.
  Failures are swallowed so they never kill the daemon.

### What this doesn't do

- No periodic refresh. Operator restarts pilkd to re-ingest, or
  explicitly calls the ``brain_ingest_claude_code`` tool from chat.
- No ChatGPT auto-ingest — ChatGPT exports aren't local, the
  operator drops a zip in the workspace on their schedule. Auto-
  ingest on boot would just hit "no file" every time.
- No Obsidian graph rebuild. The vault is plain markdown files;
  Obsidian will pick up the new notes the next time the operator
  opens the vault.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.brain import Vault
from core.integrations.ingesters.claude_code import (
    DEFAULT_ROOT as CLAUDE_DEFAULT_ROOT,
)
from core.integrations.ingesters.claude_code import (
    render_project_note,
    scan_projects,
)
from core.logging import get_logger

log = get_logger("pilkd.brain.auto_ingest")

# Soft cap so an operator with a big ~/.claude history doesn't
# produce thousands of notes on the first boot. Operators who really
# want more can call the tool from chat with a higher
# ``max_projects``.
DEFAULT_MAX_PROJECTS = 50


async def run_once(
    vault: Vault,
    *,
    root: Path = CLAUDE_DEFAULT_ROOT,
    max_projects: int = DEFAULT_MAX_PROJECTS,
) -> dict[str, int | str | list[str]]:
    """Sync-ish body of the ingest task. Async only to match the
    asyncio.create_task signature used in the lifespan; all the
    actual work is blocking file-system reads, so we keep it simple.

    Returns a summary dict for tests + the startup log line.
    """
    summary: dict[str, int | str | list[str]] = {
        "root": str(root),
        "projects_scanned": 0,
        "written": 0,
        "errors": [],
    }
    try:
        projects = scan_projects(root)
    except OSError as e:
        log.warning("brain_auto_ingest_scan_failed", error=str(e))
        summary["errors"] = [f"scan failed: {e}"]
        return summary
    projects = projects[: max(1, int(max_projects))]
    summary["projects_scanned"] = len(projects)
    if not projects:
        return summary
    written = 0
    errors: list[str] = []
    for p in projects:
        note = render_project_note(p)
        try:
            vault.write(note.path, note.body)
            written += 1
        except (OSError, ValueError) as e:
            errors.append(f"{p.slug}: {e}")
    summary["written"] = written
    summary["errors"] = errors
    return summary


def spawn(
    vault: Vault,
    *,
    root: Path = CLAUDE_DEFAULT_ROOT,
    max_projects: int = DEFAULT_MAX_PROJECTS,
) -> asyncio.Task:
    """Fire-and-forget launcher. Caller gets back the Task handle so
    it can be ``await``ed in tests; production code just drops the
    reference and lets the event loop reap it."""
    return asyncio.create_task(
        _wrapped(vault, root=root, max_projects=max_projects),
        name="brain-auto-ingest",
    )


async def _wrapped(
    vault: Vault,
    *,
    root: Path,
    max_projects: int,
) -> dict[str, int | str | list[str]]:
    log.info(
        "brain_auto_ingest_started",
        vault=str(vault.root),
        root=str(root),
        max_projects=max_projects,
    )
    try:
        summary = await run_once(
            vault, root=root, max_projects=max_projects,
        )
    except Exception as e:  # never kill the daemon
        log.exception("brain_auto_ingest_failed", error=str(e))
        return {"error": str(e)}
    log.info(
        "brain_auto_ingest_completed",
        scanned=summary.get("projects_scanned"),
        written=summary.get("written"),
        error_count=len(summary.get("errors") or []),
    )
    return summary


# ── Gmail ingest on boot ──────────────────────────────────────────
#
# Separate from the Claude Code auto-ingest above because Gmail is
# (a) network-bound, (b) much larger by default, and (c) depends on
# the user having linked their Google account. Default off — the
# operator opts in via PILK_BRAIN_AUTO_INGEST_GMAIL_ON_BOOT=true.


async def run_gmail_once(
    vault: Vault,
    creds_loader: Any,
    *,
    query: str | None = None,
    max_threads: int | None = None,
) -> dict[str, Any]:
    """Pull the operator's Gmail inbox into the brain. ``creds_loader``
    is a zero-arg callable that returns ``(creds, account)`` — same
    shape make_gmail_tools uses. We accept it as a loader rather than
    a concrete creds object so the caller isn't forced to crash at
    boot if the user hasn't linked Google yet."""
    from core.integrations.ingesters.gmail import (
        DEFAULT_MAX_THREADS,
        DEFAULT_QUERY,
        render_thread_note,
        scan_threads,
    )

    summary: dict[str, Any] = {
        "query": query or DEFAULT_QUERY,
        "threads_scanned": 0,
        "written": 0,
        "errors": [],
    }
    try:
        creds, _account = creds_loader()
    except Exception as e:  # loader should never kill boot
        summary["errors"] = [f"creds loader failed: {e}"]
        return summary
    if creds is None:
        summary["errors"] = ["google user account not linked"]
        return summary

    effective_query = query or DEFAULT_QUERY
    effective_max = int(max_threads or DEFAULT_MAX_THREADS)
    try:
        threads = await asyncio.to_thread(
            scan_threads,
            creds,
            query=effective_query,
            max_threads=effective_max,
        )
    except Exception as e:
        log.warning("brain_auto_ingest_gmail_scan_failed", error=str(e))
        summary["errors"] = [f"scan failed: {e}"]
        return summary
    summary["threads_scanned"] = len(threads)
    if not threads:
        return summary
    written = 0
    errors: list[str] = []
    for thread in threads:
        note = render_thread_note(thread)
        try:
            vault.write(note.path, note.body)
            written += 1
        except (OSError, ValueError) as e:
            errors.append(f"{thread.thread_id}: {e}")
    summary["written"] = written
    summary["errors"] = errors
    return summary


def spawn_gmail(
    vault: Vault,
    creds_loader: Any,
    *,
    query: str | None = None,
    max_threads: int | None = None,
) -> asyncio.Task:
    return asyncio.create_task(
        _wrapped_gmail(
            vault, creds_loader, query=query, max_threads=max_threads,
        ),
        name="brain-auto-ingest-gmail",
    )


async def _wrapped_gmail(
    vault: Vault,
    creds_loader: Any,
    *,
    query: str | None,
    max_threads: int | None,
) -> dict[str, Any]:
    log.info(
        "brain_auto_ingest_gmail_started",
        vault=str(vault.root),
        query=query or "default",
    )
    try:
        summary = await run_gmail_once(
            vault, creds_loader, query=query, max_threads=max_threads,
        )
    except Exception as e:
        log.exception("brain_auto_ingest_gmail_failed", error=str(e))
        return {"error": str(e)}
    log.info(
        "brain_auto_ingest_gmail_completed",
        scanned=summary.get("threads_scanned"),
        written=summary.get("written"),
        error_count=len(summary.get("errors") or []),
    )
    return summary


__all__ = [
    "DEFAULT_MAX_PROJECTS",
    "run_gmail_once",
    "run_once",
    "spawn",
    "spawn_gmail",
]
