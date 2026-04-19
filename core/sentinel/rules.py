"""Tier 0 detection rules — pure Python, no LLM, no network.

A rule is a coroutine matching :class:`Rule`:

    async def my_rule(ctx: RuleContext) -> list[Finding]: ...

It receives the shared :class:`RuleContext` (stores, recent events,
sliding log buffer) and returns zero or more :class:`Finding` objects.

Registration is decorator-based — drop a new rule into this module (or
any module that calls :func:`register_rule`) and the supervisor picks
it up automatically. The supervisor invokes :func:`run_rules` on every
Hub event *and* on its 30s periodic scan; rules should be cheap enough
that running them on every tick is unremarkable.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from core.sentinel.contracts import Finding
from core.sentinel.heartbeats import HeartbeatStore

# ── Fatal-pattern catalogue ──────────────────────────────────────
#
# Regex applied to the most recent log line's message. Case-insensitive.
# Add sparingly — every entry turns a single noisy log line into an
# incident, so the bar is "clearly a crash, not a business error".
CRASH_SIGNATURES: tuple[re.Pattern[str], ...] = (
    re.compile(r"traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"uncaught exception", re.IGNORECASE),
    re.compile(r"fatal error", re.IGNORECASE),
    re.compile(r"out of memory|memoryerror", re.IGNORECASE),
    re.compile(r"segmentation fault|segfault|sigsegv", re.IGNORECASE),
    re.compile(r"asyncio\.TimeoutError", re.IGNORECASE),
    re.compile(r"unauthorized|invalid api key", re.IGNORECASE),
    re.compile(r"rate.limited|429", re.IGNORECASE),
)


@dataclass
class LogLine:
    """A single structured log record as it would appear in a log
    tail buffer. Sentinel only consumes lines the Hub broadcasts to
    it or the log-relay helper writes; it never tails raw files."""

    agent_name: str | None
    level: str
    kind: str | None
    message: str
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class RuleContext:
    """Shared inputs for a rule pass.

    ``logs_by_agent`` is a bounded sliding window per agent. It grows
    as the supervisor ingests events and evicts on a 5-minute TTL — no
    raw filesystem tail required.

    ``event`` is only populated when a rule is fired in response to a
    single Hub event; on a periodic scan it's ``None``.
    """

    heartbeats: HeartbeatStore
    logs_by_agent: dict[str, deque[LogLine]]
    event: tuple[str, dict[str, Any]] | None = None
    # Agents currently claimed by each active task_id — used by the
    # duplicate-work rule. ``None`` means the supervisor hasn't been
    # asked to track tasks and the rule is a no-op.
    claims_by_task: dict[str, set[str]] | None = None
    # Cached state.json blobs keyed by agent name for schema checks.
    agent_state_blobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # When the scan started — used so "stale" rules don't drift with
    # individual rule run times.
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


Rule = Callable[[RuleContext], Awaitable[list[Finding]]]


_RULES: list[Rule] = []


def register_rule(fn: Rule) -> Rule:
    """Decorator: attach a rule at import time.

    Keeping it a decorator means adding a rule is a one-line change
    and tests can register ad-hoc rules without monkeypatching."""
    _RULES.append(fn)
    return fn


def builtin_rules() -> list[Rule]:
    """Snapshot of registered rules at the moment of call. The
    supervisor captures this once on startup so hot-reload doesn't
    accidentally drop rules mid-scan."""
    return list(_RULES)


async def run_rules(
    ctx: RuleContext, rules: list[Rule] | None = None
) -> list[Finding]:
    """Invoke every rule in turn, swallow rule-level errors so one
    misbehaving rule can't block the rest. Returns the concatenated
    findings, deduplicated by ``dedupe_key``."""
    rules = rules if rules is not None else list(_RULES)
    out: list[Finding] = []
    seen: set[str] = set()
    for rule in rules:
        try:
            found = await rule(ctx)
        except Exception as e:
            out.append(
                Finding(
                    kind="rule_error",
                    agent_name=None,
                    summary=f"rule {rule.__name__} raised: {e}",
                    dedupe_key=f"rule_error:{rule.__name__}:{e}",
                )
            )
            continue
        for f in found:
            key = f.dedupe_key or _auto_dedupe_key(f)
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
    return out


def _auto_dedupe_key(f: Finding) -> str:
    h = hashlib.sha256(
        f"{f.kind}|{f.agent_name}|{f.summary}".encode()
    ).hexdigest()[:16]
    return h


# ── Built-in rules ───────────────────────────────────────────────


@register_rule
async def stale_heartbeat(ctx: RuleContext) -> list[Finding]:
    """Every heartbeat older than 2x interval + not 'disabled'."""
    out: list[Finding] = []
    for hb in ctx.heartbeats.iter_stale(now=ctx.now):
        try:
            last = datetime.fromisoformat(hb.last_at)
        except ValueError:
            continue
        age = (ctx.now - last).total_seconds()
        out.append(
            Finding(
                kind="stale_heartbeat",
                agent_name=hb.agent_name,
                summary=(
                    f"{hb.agent_name} heartbeat stale "
                    f"({int(age)}s old, interval={hb.interval_seconds}s)"
                ),
                details={
                    "age_seconds": int(age),
                    "interval_seconds": hb.interval_seconds,
                    "status": hb.status,
                    "last_at": hb.last_at,
                },
                dedupe_key=f"stale_heartbeat:{hb.agent_name}",
            )
        )
    return out


# ERROR_BURST: count error/critical lines per agent over a sliding window.
ERROR_BURST_WINDOW_S = 60
ERROR_BURST_THRESHOLD = 5


@register_rule
async def error_burst(ctx: RuleContext) -> list[Finding]:
    cutoff = ctx.now - timedelta(seconds=ERROR_BURST_WINDOW_S)
    out: list[Finding] = []
    for agent, lines in ctx.logs_by_agent.items():
        # Count without allocating a new list.
        count = 0
        for line in lines:
            if line.at < cutoff:
                continue
            if line.level.lower() in ("error", "critical"):
                count += 1
        if count >= ERROR_BURST_THRESHOLD:
            out.append(
                Finding(
                    kind="error_burst",
                    agent_name=agent,
                    summary=(
                        f"{agent}: {count} error-level logs in "
                        f"{ERROR_BURST_WINDOW_S}s window"
                    ),
                    details={
                        "count": count,
                        "window_seconds": ERROR_BURST_WINDOW_S,
                        "threshold": ERROR_BURST_THRESHOLD,
                    },
                    # Dedupe within a sliding window so a continuous
                    # burst creates one incident, not N.
                    dedupe_key=f"error_burst:{agent}",
                )
            )
    return out


@register_rule
async def crash_signature(ctx: RuleContext) -> list[Finding]:
    """Match every new log line against CRASH_SIGNATURES. Fires only
    on the most recent line per agent to keep a single traceback from
    producing N findings."""
    out: list[Finding] = []
    for agent, lines in ctx.logs_by_agent.items():
        if not lines:
            continue
        last = lines[-1]
        for pat in CRASH_SIGNATURES:
            if pat.search(last.message):
                out.append(
                    Finding(
                        kind="crash_signature",
                        agent_name=agent,
                        summary=(
                            f"{agent}: fatal log pattern '{pat.pattern}' "
                            f"matched"
                        ),
                        details={
                            "pattern": pat.pattern,
                            "level": last.level,
                            "message": last.message[:500],
                        },
                        # Dedupe on (agent, pattern) so repeat fires in
                        # the same burst collapse to one incident.
                        dedupe_key=f"crash_signature:{agent}:{pat.pattern}",
                    )
                )
                break
    return out


@register_rule
async def stuck_task(ctx: RuleContext) -> list[Finding]:
    """Heartbeat shows an ``active_task_id`` older than the declared
    ``stuck_task_timeout_seconds``."""
    out: list[Finding] = []
    for hb in ctx.heartbeats.list_all():
        if not hb.active_task_id or hb.status == "disabled":
            continue
        try:
            last = datetime.fromisoformat(hb.last_at)
        except ValueError:
            continue
        age = (ctx.now - last).total_seconds()
        if age > hb.stuck_task_timeout_seconds:
            out.append(
                Finding(
                    kind="stuck_task",
                    agent_name=hb.agent_name,
                    summary=(
                        f"{hb.agent_name} stuck on task "
                        f"{hb.active_task_id} for {int(age)}s "
                        f"(timeout={hb.stuck_task_timeout_seconds}s)"
                    ),
                    details={
                        "task_id": hb.active_task_id,
                        "age_seconds": int(age),
                        "timeout_seconds": hb.stuck_task_timeout_seconds,
                    },
                    dedupe_key=f"stuck_task:{hb.agent_name}:{hb.active_task_id}",
                )
            )
    return out


@register_rule
async def duplicate_work(ctx: RuleContext) -> list[Finding]:
    """Same ``task_id`` claimed by multiple agents. Supervisor must
    populate ``ctx.claims_by_task`` for this to fire."""
    if ctx.claims_by_task is None:
        return []
    out: list[Finding] = []
    for task_id, agents in ctx.claims_by_task.items():
        if len(agents) < 2:
            continue
        # Sort for a stable dedupe key.
        participants = sorted(agents)
        out.append(
            Finding(
                kind="duplicate_work",
                agent_name=None,  # a cross-agent finding has no single owner
                summary=(
                    f"task {task_id} claimed by "
                    f"{len(participants)} agents: {', '.join(participants)}"
                ),
                details={"task_id": task_id, "agents": participants},
                dedupe_key=f"duplicate_work:{task_id}",
            )
        )
    return out


# Declared shape of every agent's optional state.json — covers the
# CONTRACT clause 3 fields. Extra keys are tolerated, missing required
# keys trigger a schema_violation finding.
STATE_JSON_REQUIRED_KEYS: tuple[str, ...] = (
    "agent_name",
    "state",
    "updated_at",
)


@register_rule
async def schema_violation(ctx: RuleContext) -> list[Finding]:
    """Check every cached ``state.json`` blob against the declared
    required-key list."""
    out: list[Finding] = []
    for agent, blob in ctx.agent_state_blobs.items():
        missing = [k for k in STATE_JSON_REQUIRED_KEYS if k not in blob]
        if missing:
            out.append(
                Finding(
                    kind="schema_violation",
                    agent_name=agent,
                    summary=(
                        f"{agent} state.json missing required keys: "
                        f"{', '.join(missing)}"
                    ),
                    details={
                        "missing_keys": missing,
                        "present_keys": sorted(blob.keys()),
                    },
                    dedupe_key=f"schema_violation:{agent}",
                )
            )
    return out


BUILTIN_RULES: list[Rule] = builtin_rules()
"""Frozen snapshot of the rules that ship with Sentinel. Test modules
can pass a subset into ``run_rules`` instead of monkeypatching."""


# Helper: build a context that's convenient for ad-hoc tests + the
# supervisor's own bookkeeping.
def build_context(
    *,
    heartbeats: HeartbeatStore,
    logs_by_agent: dict[str, deque[LogLine]] | None = None,
    event: tuple[str, dict[str, Any]] | None = None,
    claims_by_task: dict[str, set[str]] | None = None,
    agent_state_blobs: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> RuleContext:
    return RuleContext(
        heartbeats=heartbeats,
        logs_by_agent=logs_by_agent or defaultdict(lambda: deque(maxlen=200)),
        event=event,
        claims_by_task=claims_by_task,
        agent_state_blobs=agent_state_blobs or {},
        now=now or datetime.now(UTC),
    )


__all__ = [
    "BUILTIN_RULES",
    "CRASH_SIGNATURES",
    "ERROR_BURST_THRESHOLD",
    "ERROR_BURST_WINDOW_S",
    "STATE_JSON_REQUIRED_KEYS",
    "LogLine",
    "Rule",
    "RuleContext",
    "build_context",
    "builtin_rules",
    "register_rule",
    "run_rules",
]
