"""Tier 1 — Haiku triage with log-hash caching.

Every :class:`Finding` that survives dedupe gets one :func:`triage`
call. The call routes through the PILK governor's **Light tier** so the
cost stays on Haiku-class pricing, and the response is constrained to a
small JSON shape to avoid runaway generations.

A local LRU caches results by ``(finding.kind, details-hash)`` for 10
minutes so a flapping signal doesn't burn tokens once per tick.

The caller can inject any async ``llm_call`` function — production
wires this to ``llm_ask_tool`` through the governor; tests inject a
canned stub and never hit the network.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.logging import get_logger
from core.sentinel.contracts import Category, Finding, Severity, TriageResult

log = get_logger("pilkd.sentinel.triage")

# LLM signature — keep narrow. Any caller must satisfy:
#     async def llm_call(prompt: str) -> str
# returning a string we'll try to parse as JSON. Producing this signature
# from the real governor is a 5-line adapter in ``supervisor.py``.
LLMCall = Callable[[str], Awaitable[str]]


TRIAGE_PROMPT = """\
You classify a single failure incident observed by the Sentinel
supervisor inside the PILK agent runtime. Reply with **one JSON
object and nothing else**. No markdown, no prose, no code fences.

Shape:
{{
  "severity": "low" | "med" | "high" | "critical",
  "category": one of: stale_heartbeat, error_burst, crash_signature,
    schema_violation, stuck_task, duplicate_work, lock_file_orphan,
    rate_limited, transient_api_error, disk_full, unknown,
  "likely_cause": "one short sentence",
  "recommended_action": "one short action phrase",
  "confidence": 0.0 to 1.0
}}

Guidance:
- Prefer ``med`` when uncertain.
- ``critical`` = the agent is taking real-world irreversible actions
  or will silently stop doing its job soon.
- Escalate to ``high`` on anything that looks like a crash.
- ``confidence`` below 0.5 means "I'd rather an operator look at this."

Incident:
agent={agent}
finding_kind={kind}
summary={summary}
details={details}
recent_logs:
{logs}
"""


@dataclass(frozen=True)
class _CachedTriage:
    result: TriageResult
    expires_at: float


class TriageCache:
    """Tiny in-memory cache. Capacity 256 keeps memory bounded even
    under a log flood; TTL 10 minutes matches typical failure-window
    repeats."""

    def __init__(self, capacity: int = 256, ttl_seconds: float = 600.0) -> None:
        self._cap = capacity
        self._ttl = ttl_seconds
        self._data: dict[str, _CachedTriage] = {}

    def get(self, key: str) -> TriageResult | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.time():
            self._data.pop(key, None)
            return None
        return entry.result

    def put(self, key: str, result: TriageResult) -> None:
        if len(self._data) >= self._cap:
            # Cheap eviction — pop arbitrary keys until we're under cap.
            # Good enough for a rarely-full cache.
            for _ in range(max(1, self._cap // 8)):
                try:
                    self._data.pop(next(iter(self._data)))
                except StopIteration:
                    break
        self._data[key] = _CachedTriage(
            result=result, expires_at=time.time() + self._ttl
        )


def cache_key(finding: Finding, recent_logs: list[str]) -> str:
    payload = json.dumps(
        {
            "kind": finding.kind,
            "agent": finding.agent_name,
            "details": finding.details,
            # Hash over the last ~5 log lines to keep repeated identical
            # failures on the same cache entry.
            "logs": recent_logs[-5:],
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _default_triage(finding: Finding) -> TriageResult:
    """Heuristic fallback for when the LLM is unavailable or returns
    unparseable JSON. Errs on the side of escalation."""
    # Map known rule kinds to coarse defaults.
    mapping: dict[str, tuple[Severity, Category]] = {
        "stale_heartbeat": (Severity.HIGH, Category.STALE_HEARTBEAT),
        "error_burst": (Severity.MED, Category.ERROR_BURST),
        "crash_signature": (Severity.HIGH, Category.CRASH_SIGNATURE),
        "schema_violation": (Severity.MED, Category.SCHEMA_VIOLATION),
        "stuck_task": (Severity.HIGH, Category.STUCK_TASK),
        "duplicate_work": (Severity.MED, Category.DUPLICATE_WORK),
    }
    sev, cat = mapping.get(finding.kind, (Severity.MED, Category.UNKNOWN))
    return TriageResult(
        severity=sev,
        category=cat,
        likely_cause="tier-1 triage unavailable",
        recommended_action="review manually",
        confidence=0.3,
    )


async def triage(
    finding: Finding,
    *,
    recent_logs: list[str],
    llm_call: LLMCall | None,
    cache: TriageCache | None = None,
) -> TriageResult:
    """Classify ``finding``. ``llm_call`` = None → heuristic fallback."""
    key = cache_key(finding, recent_logs)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached

    if llm_call is None:
        result = _default_triage(finding)
        if cache is not None:
            cache.put(key, result)
        return result

    prompt = TRIAGE_PROMPT.format(
        agent=finding.agent_name or "(unknown)",
        kind=finding.kind,
        summary=finding.summary,
        details=json.dumps(finding.details, default=str)[:2000],
        logs="\n".join(recent_logs[-10:]) or "(no logs)",
    )
    try:
        raw = await llm_call(prompt)
    except Exception as e:
        log.warning("triage_llm_failed", error=str(e))
        result = _default_triage(finding)
        if cache is not None:
            cache.put(key, result)
        return result

    result = _parse_triage_json(raw) or _default_triage(finding)
    if cache is not None:
        cache.put(key, result)
    return result


def _parse_triage_json(raw: str) -> TriageResult | None:
    """Accept either a bare JSON object or a fenced one. Return None
    on any parse error — caller falls back to heuristic."""
    if not raw:
        return None
    text = raw.strip()
    # Strip common LLM fences just in case the model ignored us.
    if text.startswith("```"):
        text = text.strip("`")
        # Drop the language tag line if present.
        if "\n" in text:
            text = text.split("\n", 1)[1]
    # Clip to the outermost braces so trailing prose doesn't break us.
    first = text.find("{")
    last = text.rfind("}")
    if first < 0 or last < 0 or last <= first:
        return None
    try:
        d = json.loads(text[first : last + 1])
    except json.JSONDecodeError:
        return None
    try:
        return TriageResult(
            severity=Severity.parse(str(d.get("severity", "med"))),
            category=Category.parse(str(d.get("category", "unknown"))),
            likely_cause=str(d.get("likely_cause", ""))[:300],
            recommended_action=str(d.get("recommended_action", ""))[:200],
            confidence=float(d.get("confidence", 0.5)),
        )
    except (TypeError, ValueError):
        return None


__all__ = [
    "TRIAGE_PROMPT",
    "LLMCall",
    "TriageCache",
    "cache_key",
    "triage",
]
