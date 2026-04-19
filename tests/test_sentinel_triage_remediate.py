"""Triage + remediate tests. No network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.sentinel.contracts import (
    Category,
    Finding,
    Severity,
    TriageResult,
)
from core.sentinel.heartbeats import HeartbeatStore
from core.sentinel.notify import Notifier
from core.sentinel.remediate import (
    ALLOWED_REMEDIATIONS,
    RemediationResult,
    maybe_remediate,
)
from core.sentinel.triage import (
    TriageCache,
    _default_triage,
    _parse_triage_json,
    cache_key,
    triage,
)

# ── triage ────────────────────────────────────────────────────


def test_parse_triage_json_strict() -> None:
    out = _parse_triage_json(
        json.dumps(
            {
                "severity": "critical",
                "category": "crash_signature",
                "likely_cause": "oom",
                "recommended_action": "restart",
                "confidence": 0.92,
            }
        )
    )
    assert out is not None
    assert out.severity == Severity.CRITICAL
    assert out.category == Category.CRASH_SIGNATURE
    assert out.confidence == pytest.approx(0.92)


def test_parse_triage_json_strips_fences() -> None:
    raw = (
        "```json\n"
        '{"severity":"low","category":"unknown","likely_cause":"",'
        '"recommended_action":"","confidence":0.4}'
        "\n```"
    )
    out = _parse_triage_json(raw)
    assert out is not None
    assert out.severity == Severity.LOW


def test_parse_triage_json_garbage_returns_none() -> None:
    assert _parse_triage_json("totally not json") is None


def test_parse_triage_json_unknown_severity_maps_to_med() -> None:
    out = _parse_triage_json(
        '{"severity": "wat", "category": "unknown", "likely_cause": "x",'
        ' "recommended_action": "y", "confidence": 0.5}'
    )
    assert out is not None and out.severity == Severity.MED


def test_default_triage_maps_known_kinds() -> None:
    f = Finding(kind="crash_signature", agent_name="a", summary="boom")
    t = _default_triage(f)
    assert t.category == Category.CRASH_SIGNATURE
    assert t.severity == Severity.HIGH
    assert t.confidence < 0.5


@pytest.mark.asyncio
async def test_triage_uses_fallback_when_no_llm() -> None:
    f = Finding(kind="stale_heartbeat", agent_name="a", summary="x")
    out = await triage(f, recent_logs=[], llm_call=None)
    assert out.category == Category.STALE_HEARTBEAT
    assert out.severity == Severity.HIGH


@pytest.mark.asyncio
async def test_triage_llm_happy_path() -> None:
    async def fake_llm(prompt: str) -> str:
        return json.dumps(
            {
                "severity": "critical",
                "category": "crash_signature",
                "likely_cause": "oom",
                "recommended_action": "restart",
                "confidence": 0.9,
            }
        )

    out = await triage(
        Finding(kind="crash_signature", agent_name="a", summary="x"),
        recent_logs=["error: something"],
        llm_call=fake_llm,
    )
    assert out.severity == Severity.CRITICAL
    assert out.category == Category.CRASH_SIGNATURE


@pytest.mark.asyncio
async def test_triage_llm_garbage_falls_back() -> None:
    async def bad_llm(prompt: str) -> str:
        return "I'm sorry, I can't do that, Dave."

    out = await triage(
        Finding(kind="stuck_task", agent_name="a", summary="x"),
        recent_logs=[],
        llm_call=bad_llm,
    )
    assert out.category == Category.STUCK_TASK
    assert out.confidence < 0.5


@pytest.mark.asyncio
async def test_triage_llm_exception_falls_back() -> None:
    async def exploding(prompt: str) -> str:
        raise RuntimeError("api down")

    out = await triage(
        Finding(kind="stuck_task", agent_name="a", summary="x"),
        recent_logs=[],
        llm_call=exploding,
    )
    assert out.confidence < 0.5  # heuristic


@pytest.mark.asyncio
async def test_triage_cache_hits_skip_llm() -> None:
    calls = {"n": 0}

    async def counting_llm(prompt: str) -> str:
        calls["n"] += 1
        return json.dumps(
            {
                "severity": "med",
                "category": "error_burst",
                "likely_cause": "x",
                "recommended_action": "y",
                "confidence": 0.8,
            }
        )

    cache = TriageCache()
    f = Finding(kind="error_burst", agent_name="a", summary="x")
    await triage(f, recent_logs=["a"], llm_call=counting_llm, cache=cache)
    await triage(f, recent_logs=["a"], llm_call=counting_llm, cache=cache)
    assert calls["n"] == 1


def test_cache_key_is_stable() -> None:
    f = Finding(kind="x", agent_name="a", summary="s", details={"n": 1})
    k1 = cache_key(f, ["a", "b"])
    k2 = cache_key(f, ["a", "b"])
    assert k1 == k2
    assert len(k1) == 32


# ── remediate ─────────────────────────────────────────────────


class _Env:
    def __init__(self, tmp: Path, restart_ok: bool = True) -> None:
        from core.db.migrations import ensure_schema

        db = tmp / "pilk.db"
        ensure_schema(db)
        self.heartbeats = HeartbeatStore(db)
        self.logs_dir = tmp / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._ok = restart_ok

    async def restart_agent(self, agent_name: str) -> RemediationResult:
        return RemediationResult(
            kind="restarted",
            ok=self._ok,
            message=f"mock-restart {agent_name}",
        )


def _high_triage(cat: Category) -> TriageResult:
    return TriageResult(
        severity=Severity.HIGH,
        category=cat,
        likely_cause="",
        recommended_action="",
        confidence=0.9,
    )


@pytest.mark.asyncio
async def test_allowed_remediation_covers_expected_categories() -> None:
    assert Category.STALE_HEARTBEAT in ALLOWED_REMEDIATIONS
    assert Category.LOCK_FILE_ORPHAN in ALLOWED_REMEDIATIONS
    assert Category.RATE_LIMITED in ALLOWED_REMEDIATIONS
    assert Category.TRANSIENT_API_ERROR in ALLOWED_REMEDIATIONS
    assert Category.DISK_FULL in ALLOWED_REMEDIATIONS


@pytest.mark.asyncio
async def test_remediation_off_allowlist_returns_none(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    result = await maybe_remediate(
        Finding(kind="x", agent_name="a", summary="x"),
        _high_triage(Category.UNKNOWN),
        env,
    )
    assert result is None


@pytest.mark.asyncio
async def test_low_confidence_skips_remediation(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    low = TriageResult(
        severity=Severity.HIGH,
        category=Category.STALE_HEARTBEAT,
        likely_cause="",
        recommended_action="",
        confidence=0.2,
    )
    result = await maybe_remediate(
        Finding(kind="x", agent_name="a", summary="x"), low, env
    )
    assert result is None


@pytest.mark.asyncio
async def test_stale_heartbeat_triggers_restart(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    result = await maybe_remediate(
        Finding(kind="stale_heartbeat", agent_name="a", summary="x"),
        _high_triage(Category.STALE_HEARTBEAT),
        env,
    )
    assert result is not None
    assert result.kind == "restarted"
    assert result.ok is True


@pytest.mark.asyncio
async def test_stale_heartbeat_without_agent_name_refuses(
    tmp_path: Path,
) -> None:
    env = _Env(tmp_path)
    result = await maybe_remediate(
        Finding(kind="stale_heartbeat", agent_name=None, summary="x"),
        _high_triage(Category.STALE_HEARTBEAT),
        env,
    )
    assert result is not None and result.ok is False


@pytest.mark.asyncio
async def test_clear_lock_file(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    lock = tmp_path / "stuck.lock"
    lock.write_text("")
    result = await maybe_remediate(
        Finding(
            kind="lock_orphan",
            agent_name="a",
            summary="x",
            details={"lock_path": str(lock)},
        ),
        _high_triage(Category.LOCK_FILE_ORPHAN),
        env,
    )
    assert result is not None and result.ok is True
    assert not lock.exists()


@pytest.mark.asyncio
async def test_rotate_old_logs_compresses_stale_files(tmp_path: Path) -> None:
    import os
    import time

    env = _Env(tmp_path)
    old = env.logs_dir / "old.log"
    fresh = env.logs_dir / "fresh.log"
    old.write_text("old")
    fresh.write_text("fresh")
    # Backdate the old log 10 days.
    past = time.time() - (10 * 86400)
    os.utime(old, (past, past))
    result = await maybe_remediate(
        Finding(kind="disk_full", agent_name=None, summary="x"),
        _high_triage(Category.DISK_FULL),
        env,
    )
    assert result is not None and result.ok is True
    assert not old.exists()
    assert (env.logs_dir / "old.log.gz").exists()
    # Fresh log is untouched.
    assert fresh.exists()


# ── notify ────────────────────────────────────────────────────


def test_notifier_disabled_without_url() -> None:
    n = Notifier(webhook_url=None)
    assert n.enabled is False


def test_notifier_severity_gate() -> None:
    n = Notifier(webhook_url="http://example.test", min_severity=Severity.HIGH)
    assert n.should_notify(Severity.LOW) is False
    assert n.should_notify(Severity.MED) is False
    assert n.should_notify(Severity.HIGH) is True
    assert n.should_notify(Severity.CRITICAL) is True
