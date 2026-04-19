"""Sentinel orchestrator.

Wires Hub events → rules → triage → remediate → notify. Also owns
the 30s periodic heartbeat scan (the only non-event trigger) and the
sliding log buffer every rule reads from.

Design notes that matter for review:

* **One task.** The supervisor runs a single ``asyncio.Task`` that
  handles periodic scans. Hub events arrive via the ``on_event``
  coroutine which the Hub's broadcast loop awaits directly. No queue,
  no background worker pool — the work each event does is small and
  we want backpressure on runaway signals rather than buffered token
  burn.

* **Dedupe window.** Findings with the same ``dedupe_key`` collapse
  within :data:`DEDUPE_WINDOW_SECONDS`. Prevents a flapping signal
  from creating N incidents.

* **Daily token ceiling.** :class:`Supervisor` refuses to call triage
  past :data:`DEFAULT_DAILY_TOKEN_LIMIT` per 24h. When it trips, every
  subsequent finding uses the heuristic fallback — we'd rather miss a
  Haiku nuance than invent budget.

* **No external I/O on hot path.** ``Notifier.notify`` is called via
  ``asyncio.to_thread`` so a slow webhook can't delay the next rule
  pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging import get_logger
from core.sentinel.contracts import (
    Finding,
    Incident,
)
from core.sentinel.heartbeats import HeartbeatStore
from core.sentinel.incidents import IncidentStore
from core.sentinel.notify import Notifier
from core.sentinel.remediate import (
    RemediationEnv,
    RemediationResult,
    maybe_remediate,
)
from core.sentinel.rules import (
    BUILTIN_RULES,
    LogLine,
    Rule,
    RuleContext,
    run_rules,
)
from core.sentinel.triage import LLMCall, TriageCache, triage

log = get_logger("pilkd.sentinel")

DEFAULT_SCAN_INTERVAL_SECONDS = 30
DEDUPE_WINDOW_SECONDS = 300  # 5 minutes
DEFAULT_LOG_BUFFER_PER_AGENT = 200
# Upper bound on Sentinel's *own* token spend per 24h. Tested + cheap
# to hit ~5k under typical load per CONTRACT; 50k gives us 10x headroom
# before self-throttling kicks in.
DEFAULT_DAILY_TOKEN_LIMIT = 50_000


RestartFn = Callable[[str], Awaitable[RemediationResult]]
"""Callable the supervisor uses when a remediation decides to restart
an agent. Wired in the FastAPI lifespan against the AgentRegistry."""


BroadcastFn = Callable[[str, Any], Awaitable[None]]
"""Optional broadcaster the supervisor calls on every created incident
so the UI + the orchestrator can react without polling. Shape matches
the Hub's ``broadcast(event, payload)`` signature. ``None`` keeps the
pre-existing "sentinel is silent externally" behaviour."""


@dataclass
class _TokenLedger:
    """In-memory per-day token accumulator. Resets on UTC day rollover.
    Rough — we only track input+output estimates from triage calls —
    but enough to catch a runaway watchdog."""

    day: str = field(default_factory=lambda: datetime.now(UTC).date().isoformat())
    total: int = 0

    def roll(self) -> None:
        today = datetime.now(UTC).date().isoformat()
        if today != self.day:
            self.day = today
            self.total = 0

    def add(self, n: int) -> None:
        self.roll()
        self.total += max(0, int(n))

    def would_exceed(self, limit: int, headroom: int = 500) -> bool:
        self.roll()
        return (self.total + headroom) >= limit


@dataclass
class _DefaultEnv:
    """Concrete :class:`RemediationEnv` wired with a restart callable."""

    heartbeats: HeartbeatStore
    logs_dir: Path
    _restart: RestartFn

    async def restart_agent(self, agent_name: str) -> RemediationResult:
        return await self._restart(agent_name)


class Supervisor:
    def __init__(
        self,
        *,
        heartbeats: HeartbeatStore,
        incidents: IncidentStore,
        notifier: Notifier | None = None,
        rules: list[Rule] | None = None,
        restart_fn: RestartFn | None = None,
        logs_dir: Path,
        llm_call: LLMCall | None = None,
        scan_interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
        log_buffer_per_agent: int = DEFAULT_LOG_BUFFER_PER_AGENT,
        daily_token_limit: int = DEFAULT_DAILY_TOKEN_LIMIT,
        broadcast: BroadcastFn | None = None,
    ) -> None:
        self._heartbeats = heartbeats
        self._incidents = incidents
        self._notifier = notifier or Notifier()
        self._rules = rules if rules is not None else list(BUILTIN_RULES)
        self._restart_fn = restart_fn or _noop_restart
        self._logs_dir = logs_dir
        self._llm_call = llm_call
        self._broadcast = broadcast
        self._triage_cache = TriageCache()
        self._scan_interval = scan_interval_seconds
        self._log_buffer_per_agent = log_buffer_per_agent
        self._token_limit = daily_token_limit
        self._tokens = _TokenLedger()

        self._logs_by_agent: dict[str, deque[LogLine]] = defaultdict(
            lambda: deque(maxlen=log_buffer_per_agent)
        )
        self._agent_state_blobs: dict[str, dict[str, Any]] = {}
        self._claims_by_task: dict[str, set[str]] = defaultdict(set)
        self._dedupe: dict[str, float] = {}

        self._scan_task: asyncio.Task[None] | None = None

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        if self._scan_task is not None and not self._scan_task.done():
            return
        self._scan_task = asyncio.create_task(
            self._scan_forever(), name="sentinel.scan"
        )
        log.info(
            "sentinel_started",
            scan_interval_s=self._scan_interval,
            rules=len(self._rules),
        )

    async def stop(self) -> None:
        if self._scan_task is not None:
            self._scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._scan_task
            self._scan_task = None
        log.info("sentinel_stopped")

    # ── Hub hook ──────────────────────────────────────────────

    async def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Called by :class:`Hub.broadcast`. Ingest the event into
        internal state and immediately run the event-scoped rule pass."""
        self._ingest_event(event_type, payload)
        await self._scan(event=(event_type, payload))

    # ── Public tool-facing helpers ────────────────────────────

    def status(self) -> list[dict[str, Any]]:
        """One-line health for every known agent. The
        ``sentinel_status`` tool surfaces this to the operator."""
        out: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        for hb in self._heartbeats.list_all():
            try:
                last = datetime.fromisoformat(hb.last_at)
                age = int((now - last).total_seconds())
            except ValueError:
                age = -1
            out.append(
                {
                    "agent_name": hb.agent_name,
                    "status": hb.status,
                    "progress": hb.progress,
                    "active_task_id": hb.active_task_id,
                    "age_seconds": age,
                    "interval_seconds": hb.interval_seconds,
                    "last_at": hb.last_at,
                    "stale": (
                        age >= 0
                        and age > 2 * hb.interval_seconds
                        and hb.status != "disabled"
                    ),
                }
            )
        return out

    def token_spend_today(self) -> dict[str, int]:
        self._tokens.roll()
        return {"day": 0, "total": self._tokens.total, "limit": self._token_limit}
        # (kept "day: 0" for back-compat with an earlier caller shape;
        # real date string lives in ``self._tokens.day`` and is written
        # by ``_snapshot_tokens`` below.)

    def snapshot(self) -> dict[str, Any]:
        self._tokens.roll()
        return {
            "rules": [r.__name__ for r in self._rules],
            "dedupe_window_seconds": DEDUPE_WINDOW_SECONDS,
            "scan_interval_seconds": self._scan_interval,
            "token_spend": {
                "day": self._tokens.day,
                "total": self._tokens.total,
                "limit": self._token_limit,
            },
            "webhook_enabled": self._notifier.enabled,
        }

    # ── Internals ─────────────────────────────────────────────

    def _ingest_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Extract the bits of an event that feed the rule engine.

        Known shapes (any missing field is treated as unknown):

            plan.step_updated    { agent, status, task_id, message, level }
            plan.completed       { agent, status, task_id, error? }
            agent.created        { name }
            cost.updated         { agent, kind, detail, ... }

        Sentinel is defensive about schema drift — if a field is absent
        we simply skip that ingestion. Better to miss one signal than
        crash on a new event shape."""
        agent = (
            payload.get("agent")
            or payload.get("agent_name")
            or payload.get("name")
        )
        message = payload.get("message") or payload.get("error") or ""
        level = str(payload.get("level") or _infer_level(event_type, payload))
        # Log buffer
        if agent:
            line = LogLine(
                agent_name=str(agent),
                level=level,
                kind=event_type,
                message=str(message),
            )
            self._logs_by_agent[str(agent)].append(line)

        # Track task claims for the duplicate-work rule.
        task_id = payload.get("task_id")
        if agent and task_id:
            if event_type in (
                "plan.step_updated",
                "plan.created",
            ) and payload.get("status") in (None, "running", "pending"):
                self._claims_by_task[str(task_id)].add(str(agent))
            if event_type == "plan.completed":
                self._claims_by_task.pop(str(task_id), None)

        # state.json blobs arrive on a dedicated event type so agents
        # can opt in without changing the plan protocol.
        if event_type == "agent.state_blob" and agent and isinstance(
            payload.get("blob"), dict
        ):
            self._agent_state_blobs[str(agent)] = dict(payload["blob"])

    async def _scan_forever(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._scan_interval)
                try:
                    await self._scan(event=None)
                except Exception as e:
                    log.warning("sentinel_scan_error", error=str(e))
        except asyncio.CancelledError:
            return

    async def _scan(
        self, *, event: tuple[str, dict[str, Any]] | None
    ) -> list[Incident]:
        ctx = RuleContext(
            heartbeats=self._heartbeats,
            logs_by_agent=self._logs_by_agent,
            event=event,
            claims_by_task=self._claims_by_task,
            agent_state_blobs=self._agent_state_blobs,
            now=datetime.now(UTC),
        )
        findings = await run_rules(ctx, rules=self._rules)
        if not findings:
            return []
        created: list[Incident] = []
        for finding in findings:
            inc = await self._handle_finding(finding)
            if inc is not None:
                created.append(inc)
        return created

    async def _handle_finding(self, finding: Finding) -> Incident | None:
        if self._is_duplicate(finding):
            return None

        # Pull the last ~10 log lines for the relevant agent to feed
        # into triage as context.
        logs: list[str] = []
        if finding.agent_name:
            lines = self._logs_by_agent.get(finding.agent_name, deque())
            logs = [f"{line.level}: {line.message}" for line in list(lines)[-10:]]

        triage_call: LLMCall | None = self._llm_call
        if self._tokens.would_exceed(self._token_limit):
            log.warning(
                "sentinel_token_ceiling_reached",
                limit=self._token_limit,
                spent=self._tokens.total,
            )
            triage_call = None  # fall back to heuristic

        tresult = await triage(
            finding,
            recent_logs=logs,
            llm_call=triage_call,
            cache=self._triage_cache,
        )
        # Rough token accounting — prompt is ~300 tokens + short
        # response ~80. Undercounting is fine; overcounting is safe.
        if triage_call is not None:
            self._tokens.add(400)

        remediation = await maybe_remediate(
            finding, tresult, self._env_for_remediate()
        )

        incident = self._incidents.create(
            finding=finding,
            triage=tresult,
            category=tresult.category,
            severity=tresult.severity,
            remediation=remediation.kind if remediation else None,
            outcome=(
                "ok"
                if remediation and remediation.ok
                else (remediation.message if remediation else None)
            ),
        )
        if remediation is not None:
            self._incidents.update_outcome(
                incident.id,
                remediation=remediation.kind,
                outcome="ok" if remediation.ok else f"failed: {remediation.message}",
            )

        if self._notifier.enabled and self._notifier.should_notify(
            incident.severity
        ):
            # Fire-and-forget in a thread so a slow webhook doesn't
            # block the next rule pass.
            await asyncio.to_thread(self._notifier.notify, incident)

        if self._broadcast is not None:
            # In-process broadcast so the UI + orchestrator learn about
            # new incidents without polling. Best-effort — a broadcaster
            # failure must never block the supervisor loop.
            try:
                await self._broadcast(
                    "sentinel.incident",
                    _incident_broadcast_payload(incident),
                )
            except Exception as e:
                log.warning(
                    "sentinel_broadcast_failed",
                    incident_id=incident.id,
                    error=str(e),
                )

        log.info(
            "sentinel_finding",
            incident_id=incident.id,
            kind=finding.kind,
            agent=finding.agent_name,
            severity=tresult.severity.value,
            category=tresult.category.value,
            remediation=remediation.kind if remediation else None,
            tier=1 if self._llm_call is not None else 0,
        )
        return incident

    def _is_duplicate(self, finding: Finding) -> bool:
        now = time.time()
        # Sweep expired entries first — cheap, and keeps memory flat.
        cutoff = now - DEDUPE_WINDOW_SECONDS
        expired = [k for k, ts in self._dedupe.items() if ts < cutoff]
        for k in expired:
            self._dedupe.pop(k, None)

        key = finding.dedupe_key or f"{finding.kind}:{finding.agent_name}"
        if key in self._dedupe:
            return True
        self._dedupe[key] = now
        return False

    def _env_for_remediate(self) -> RemediationEnv:
        return _DefaultEnv(
            heartbeats=self._heartbeats,
            logs_dir=self._logs_dir,
            _restart=self._restart_fn,
        )


async def _noop_restart(agent_name: str) -> RemediationResult:
    """Fallback restart — when the supervisor isn't wired to an agent
    registry yet, any restart attempt returns a truthful 'can't'."""
    return RemediationResult(
        kind="restarted",
        ok=False,
        message=(
            "no restart_fn wired — supervisor constructed without "
            "AgentRegistry access"
        ),
    )


def _infer_level(event_type: str, payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "").lower()
    if status in ("error", "failed", "errored"):
        return "error"
    if event_type.endswith(".error") or "error" in payload:
        return "error"
    return "info"


def _incident_broadcast_payload(incident: Incident) -> dict[str, Any]:
    """Compact payload for WebSocket subscribers + orchestrator context.
    Full details live in the DB; this shape is optimized for a top-bar
    badge + a one-line chat interjection."""
    return {
        "id": incident.id,
        "agent": incident.agent_name,
        "severity": incident.severity.value,
        "category": incident.category.value,
        "kind": incident.finding_kind,
        "summary": incident.summary,
        "likely_cause": (
            incident.triage.likely_cause if incident.triage else None
        ),
        "recommended_action": (
            incident.triage.recommended_action if incident.triage else None
        ),
        "remediation": incident.remediation,
        "outcome": incident.outcome,
        "created_at": incident.created_at,
    }


__all__ = [
    "DEDUPE_WINDOW_SECONDS",
    "DEFAULT_DAILY_TOKEN_LIMIT",
    "DEFAULT_SCAN_INTERVAL_SECONDS",
    "BroadcastFn",
    "RestartFn",
    "Supervisor",
]
