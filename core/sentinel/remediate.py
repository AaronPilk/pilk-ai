"""Tier 2 — bounded auto-remediation.

The remediation layer is deliberately narrow. Only categories in
:data:`ALLOWED_REMEDIATIONS` ever get an automatic fix; everything else
becomes a notification. This is the **single chokepoint** for the
"bounded autonomy" principle from :mod:`core.sentinel`.

Every remediation is a coroutine with the signature:

    async def remediate(finding, triage, env) -> RemediationResult

``env`` carries the process-wide handles a remediation might need
(agent registry for restart, heartbeat store for reset, ledger for
logging). We inject rather than import so tests can pass a stub.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.logging import get_logger
from core.sentinel.contracts import Category, Finding, TriageResult
from core.sentinel.heartbeats import HeartbeatStore

log = get_logger("pilkd.sentinel.remediate")


@dataclass(frozen=True)
class RemediationResult:
    kind: str  # e.g. "restarted" | "retried" | "cleared_lock"
    ok: bool
    message: str


class RemediationEnv(Protocol):
    """Minimal interface exposed to remediators. The supervisor passes
    a concrete object that satisfies this; tests pass a lightweight
    shim. Keeping it a Protocol avoids a real import of app state from
    the remediate layer."""

    heartbeats: HeartbeatStore
    logs_dir: Path

    async def restart_agent(self, agent_name: str) -> RemediationResult: ...


# ── Remediation implementations ─────────────────────────────────


async def _restart_stale(
    finding: Finding, triage: TriageResult, env: RemediationEnv
) -> RemediationResult:
    agent = finding.agent_name
    if not agent:
        return RemediationResult(
            kind="restart_agent",
            ok=False,
            message="no agent_name on finding",
        )
    return await env.restart_agent(agent)


async def _clear_lock_file(
    finding: Finding, triage: TriageResult, env: RemediationEnv
) -> RemediationResult:
    path = finding.details.get("lock_path")
    if not path:
        return RemediationResult(
            kind="cleared_lock",
            ok=False,
            message="no 'lock_path' in finding.details",
        )
    p = Path(str(path))
    try:
        if p.exists():
            p.unlink()
            return RemediationResult(
                kind="cleared_lock",
                ok=True,
                message=f"deleted {p}",
            )
        return RemediationResult(
            kind="cleared_lock",
            ok=False,
            message=f"{p} did not exist",
        )
    except OSError as e:
        return RemediationResult(
            kind="cleared_lock",
            ok=False,
            message=f"unlink failed: {e}",
        )


# Exponential backoff state — keyed by (agent, category). Per-process,
# resets on restart. Good enough for a watchdog; real distributed rate
# limiting belongs in the calling agent's retry loop.
_backoff_by_key: dict[str, tuple[int, float]] = {}


async def _backoff_retry(
    finding: Finding, triage: TriageResult, env: RemediationEnv
) -> RemediationResult:
    key = f"{finding.agent_name}:{triage.category.value}"
    attempt, _last = _backoff_by_key.get(key, (0, 0.0))
    attempt += 1
    # 2^n seconds, jittered ±25%, capped at 2 minutes.
    base = min(2**attempt, 120)
    jitter = base * 0.25 * (2 * ((time.time() * 997) % 1.0) - 1)
    delay = max(1.0, base + jitter)
    _backoff_by_key[key] = (attempt, time.time())

    # Sentinel doesn't perform the retry itself — the agent owns its
    # retry loop. We just ack the decision + let the agent observe the
    # incident. If attempt > 3 we give up and notify.
    if attempt > 3:
        _backoff_by_key.pop(key, None)
        return RemediationResult(
            kind="retried",
            ok=False,
            message=f"gave up after {attempt - 1} backoffs",
        )
    await asyncio.sleep(0)  # keep it awaitable; real delay belongs to the agent
    return RemediationResult(
        kind="retried",
        ok=True,
        message=f"scheduled retry #{attempt} in ~{delay:.0f}s",
    )


LOG_ROTATION_AGE_DAYS = 7


async def _rotate_old_logs(
    finding: Finding, triage: TriageResult, env: RemediationEnv
) -> RemediationResult:
    """Compress logs older than LOG_ROTATION_AGE_DAYS under env.logs_dir.
    Matches the spec's ``disk_full`` remediation."""
    cutoff = time.time() - (LOG_ROTATION_AGE_DAYS * 86400)
    try:
        rotated = 0
        for path in env.logs_dir.rglob("*.log"):
            try:
                if path.stat().st_mtime > cutoff:
                    continue
                gz = path.with_suffix(path.suffix + ".gz")
                if gz.exists():
                    continue
                # Use shutil to gzip + remove original.
                import gzip

                with path.open("rb") as src, gzip.open(gz, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                path.unlink()
                rotated += 1
            except OSError:
                continue
        return RemediationResult(
            kind="rotated_logs",
            ok=True,
            message=f"rotated {rotated} log files older than "
            f"{LOG_ROTATION_AGE_DAYS}d",
        )
    except Exception as e:
        return RemediationResult(
            kind="rotated_logs",
            ok=False,
            message=f"rotation failed: {e}",
        )


RemediationFn = Callable[
    [Finding, TriageResult, RemediationEnv], Awaitable[RemediationResult]
]


ALLOWED_REMEDIATIONS: dict[Category, RemediationFn] = {
    Category.STALE_HEARTBEAT: _restart_stale,
    Category.LOCK_FILE_ORPHAN: _clear_lock_file,
    Category.RATE_LIMITED: _backoff_retry,
    Category.TRANSIENT_API_ERROR: _backoff_retry,
    Category.DISK_FULL: _rotate_old_logs,
}
"""The full auto-remediation allowlist. Any category not here goes
straight to notify — no exceptions, no magic overrides. Adding a
category means updating this dict *and* reviewing the function for
idempotency + no-surprise behavior."""


async def maybe_remediate(
    finding: Finding, triage: TriageResult, env: RemediationEnv
) -> RemediationResult | None:
    """Dispatch iff the triaged category is in the allowlist. Returns
    None when no remediation is allowed — caller must then notify."""
    fn = ALLOWED_REMEDIATIONS.get(triage.category)
    if fn is None:
        return None
    # Low-confidence triage never auto-remediates. Surface it instead.
    if triage.confidence < 0.5:
        return None
    try:
        return await fn(finding, triage, env)
    except Exception as e:
        log.warning(
            "remediation_failed",
            category=triage.category.value,
            error=str(e),
        )
        return RemediationResult(
            kind=triage.category.value,
            ok=False,
            message=f"remediator raised: {e}",
        )


__all__ = [
    "ALLOWED_REMEDIATIONS",
    "LOG_ROTATION_AGE_DAYS",
    "RemediationEnv",
    "RemediationFn",
    "RemediationResult",
    "maybe_remediate",
]
