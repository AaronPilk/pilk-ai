"""Tests for the IRREVERSIBLE computer_* tool family + the shared
ComputerControlGate. Every test clears the singleton gate so cases
don't bleed shared state. Path-block tests use a fake $HOME so we
don't rely on machine-specific hierarchies."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.config import get_settings
from core.policy.computer_control import (
    CONFIRMATION_TOKEN_TTL_S,
    AuditEntry,
    BlockedPathError,
    ComputerControlDisabledError,
    ComputerControlGate,
    DailyLimitExceededError,
    TokenRequiredError,
    build_default_gate,
    fresh_audit_entry,
)
from core.policy.risk import RiskClass
from core.tools.builtin.computer_control import (
    COMPUTER_CONTROL_TOOLS,
    computer_fs_read_tool,
    computer_fs_write_tool,
    computer_osascript_tool,
    computer_shell_tool,
    reset_gate_for_tests,
    set_gate,
)
from core.tools.registry import ToolContext


def _enable(monkeypatch: pytest.MonkeyPatch, value: str = "true") -> None:
    """Flip the kill switch ON for a test."""
    get_settings.cache_clear()
    monkeypatch.setenv("COMPUTER_CONTROL_ENABLED", value)


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    for k in (
        "COMPUTER_CONTROL_ENABLED",
        "PILK_COMPUTER_CONTROL_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def gate(tmp_path: Path) -> Iterator[ComputerControlGate]:
    """A fresh gate wired to a tmp audit log + registered as the
    module singleton so the tools see it."""
    g = ComputerControlGate(
        audit_path=tmp_path / "audit.jsonl",
        daily_limit=5,
    )
    set_gate(g)
    yield g
    reset_gate_for_tests()


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(sandbox_root=None)


# ── gate: enable state ──────────────────────────────────────────


def test_gate_disabled_by_default(
    gate: ComputerControlGate, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable(monkeypatch)
    assert gate.is_enabled() is False
    with pytest.raises(ComputerControlDisabledError):
        gate.require_enabled()


@pytest.mark.parametrize(
    "raw", ["true", "TRUE", "yes", "1", "on", " true "],
)
def test_gate_enabled_by_various_truthy_strings(
    gate: ComputerControlGate, monkeypatch: pytest.MonkeyPatch, raw: str,
) -> None:
    _enable(monkeypatch, raw)
    assert gate.is_enabled() is True


@pytest.mark.parametrize(
    "raw", ["false", "no", "0", "off", "maybe", ""],
)
def test_gate_not_enabled_by_falsy_strings(
    gate: ComputerControlGate, monkeypatch: pytest.MonkeyPatch, raw: str,
) -> None:
    if raw:
        _enable(monkeypatch, raw)
    else:
        _disable(monkeypatch)
    assert gate.is_enabled() is False


# ── gate: tokens ────────────────────────────────────────────────


def test_token_is_single_use(gate: ComputerControlGate) -> None:
    tok = gate.issue_token("computer_shell", {"command": "whoami"})
    gate.verify_and_consume_token(
        "computer_shell", {"command": "whoami"}, tok.token,
    )
    with pytest.raises(TokenRequiredError):
        gate.verify_and_consume_token(
            "computer_shell", {"command": "whoami"}, tok.token,
        )


def test_token_bound_to_tool_name(gate: ComputerControlGate) -> None:
    """A token issued for computer_shell cannot be replayed against
    computer_osascript. Changing tool voids the token."""
    tok = gate.issue_token("computer_shell", {"command": "whoami"})
    with pytest.raises(TokenRequiredError):
        gate.verify_and_consume_token(
            "computer_osascript", {"command": "whoami"}, tok.token,
        )


def test_token_bound_to_args(gate: ComputerControlGate) -> None:
    """Different args = different fingerprint = token rejected."""
    tok = gate.issue_token("computer_shell", {"command": "whoami"})
    with pytest.raises(TokenRequiredError):
        gate.verify_and_consume_token(
            "computer_shell", {"command": "rm -rf ~"}, tok.token,
        )


def test_token_rejects_unknown_string(gate: ComputerControlGate) -> None:
    with pytest.raises(TokenRequiredError):
        gate.verify_and_consume_token(
            "computer_shell", {"command": "whoami"}, "garbage",
        )


def test_expired_token_rejected(gate: ComputerControlGate) -> None:
    """Age the token past its TTL and confirm the gate issues a
    fresh one instead of honouring the stale one."""
    import time as _time
    tok = gate.issue_token("computer_shell", {"command": "whoami"})
    # Artificially expire it.
    stored = gate._pending[tok.token]
    stored.expires_at = _time.time() - 1
    with pytest.raises(TokenRequiredError) as exc:
        gate.verify_and_consume_token(
            "computer_shell", {"command": "whoami"}, tok.token,
        )
    # The exception carries a FRESH token — that's how the tool
    # surfaces "try again with this new token" without the caller
    # having to request one explicitly.
    assert exc.value.token != tok.token


def test_token_ttl_is_five_minutes(gate: ComputerControlGate) -> None:
    # Sanity-check on the constant so nobody silently loosens it.
    assert CONFIRMATION_TOKEN_TTL_S == 300


# ── gate: daily limit ───────────────────────────────────────────


def test_daily_limit_trips(gate: ComputerControlGate) -> None:
    # daily_limit=5 in the fixture.
    for _ in range(5):
        gate.check_and_bump_daily()
    with pytest.raises(DailyLimitExceededError):
        gate.check_and_bump_daily()


def test_daily_limit_resets_on_date_change(
    gate: ComputerControlGate,
) -> None:
    gate.check_and_bump_daily()
    # Fake a date change by overwriting _daily directly.
    gate._daily.utc_date = "1999-01-01"
    gate._daily.count = gate.daily_limit
    # Next call sees today != 1999-01-01, wipes, and counts 1.
    n = gate.check_and_bump_daily()
    assert n == 1


def test_build_default_gate_clamps_to_hard_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("COMPUTER_CONTROL_DAILY_LIMIT", "1000")
    get_settings.cache_clear()
    g = build_default_gate(tmp_path)
    assert g.daily_limit == 100  # hard ceiling


def test_build_default_gate_clamps_to_minimum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("COMPUTER_CONTROL_DAILY_LIMIT", "-5")
    get_settings.cache_clear()
    g = build_default_gate(tmp_path)
    assert g.daily_limit == 1


# ── gate: hard-block paths ─────────────────────────────────────


@pytest.mark.parametrize(
    "prefix",
    [
        "/.ssh",
        "/.aws",
        "/.gnupg",
        "/Library/Keychains",
    ],
)
def test_home_block_prefixes(
    gate: ComputerControlGate, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, prefix: str,
) -> None:
    """Construct a fake $HOME whose subtree contains each blocked
    prefix, then assert the gate refuses paths inside it."""
    fake_home = tmp_path / "home"
    (fake_home / prefix.strip("/")).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    target = fake_home / prefix.strip("/") / "x"
    target.write_text("data")
    with pytest.raises(BlockedPathError):
        gate.check_path(target)


@pytest.mark.parametrize("prefix", ["/etc", "/System"])
def test_absolute_block_prefixes(
    gate: ComputerControlGate, prefix: str,
) -> None:
    """These are absolute paths, not home-relative — if they exist
    on the test host we still block them."""
    # We don't need the file to exist; the gate compares string
    # prefixes on the resolved path.
    with pytest.raises(BlockedPathError):
        gate.check_path(Path(prefix) / "something")


def test_home_block_symlink_escape_is_caught(
    gate: ComputerControlGate, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink under a writeable dir pointing INTO ~/.ssh must
    still be blocked — gate resolves symlinks before checking."""
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    real_secret = fake_home / ".ssh" / "id_rsa"
    real_secret.write_text("secret")
    # Symlink somewhere innocent → into ~/.ssh
    benign = fake_home / "work"
    benign.mkdir()
    tricky = benign / "innocent"
    os.symlink(real_secret, tricky)
    monkeypatch.setenv("HOME", str(fake_home))
    with pytest.raises(BlockedPathError):
        gate.check_path(tricky)


def test_unrelated_path_is_allowed(
    gate: ComputerControlGate, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    (fake_home / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # Should not raise.
    gate.check_path(fake_home / "work" / "notes.md")


# ── gate: audit log ─────────────────────────────────────────────


def test_audit_log_writes_jsonl(gate: ComputerControlGate) -> None:
    entry = fresh_audit_entry(
        "computer_shell", "ran something", outcome="ok", detail="exit=0",
    )
    gate.append_audit(entry)
    lines = gate.audit_path.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["tool"] == "computer_shell"
    assert parsed["outcome"] == "ok"
    assert parsed["detail"] == "exit=0"


def test_audit_log_append_not_overwrite(
    gate: ComputerControlGate,
) -> None:
    for i in range(3):
        gate.append_audit(
            AuditEntry(
                ts="2026-01-01",
                tool="computer_fs_read",
                args_summary=f"call {i}",
                outcome="ok",
            )
        )
    lines = gate.audit_path.read_text().splitlines()
    assert len(lines) == 3


def test_audit_log_survives_unwriteable_parent(
    tmp_path: Path,
) -> None:
    """A broken audit path must not propagate — the point is the
    tool call already succeeded; a logging failure can't pretend
    it didn't."""
    unwriteable = tmp_path / "nonexistent" / "cannot" / "exist"
    g = ComputerControlGate(audit_path=unwriteable / "audit.jsonl")
    # Make the containing dir uncreatable by making the top dir a file.
    (tmp_path / "nonexistent").write_text("i am a file, not a dir")
    g.append_audit(
        AuditEntry(ts="x", tool="t", args_summary="s", outcome="ok")
    )
    # No exception = win.


# ── tool registry ───────────────────────────────────────────────


def test_registry_shape() -> None:
    assert len(COMPUTER_CONTROL_TOOLS) == 4
    names = [t.name for t in COMPUTER_CONTROL_TOOLS]
    assert len(names) == len(set(names))
    for n in names:
        assert n.startswith("computer_")


def test_every_tool_is_irreversible() -> None:
    """The whole premise of this bundle is IRREVERSIBLE risk. If
    any of these accidentally drop to NET_WRITE the approval story
    silently degrades."""
    for t in COMPUTER_CONTROL_TOOLS:
        assert t.risk == RiskClass.IRREVERSIBLE, t.name


# ── tool preamble: disabled, token dance, daily limit ──────────


@pytest.mark.asyncio
async def test_tool_refuses_when_disabled(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable(monkeypatch)
    out = await computer_fs_read_tool.handler(
        {"path": "/tmp/x"}, ctx,
    )
    assert out.is_error
    assert "disabled" in out.content.lower() or "disabled" in out.content
    # Denied calls are audited.
    assert gate.audit_path.is_file()
    entry = json.loads(gate.audit_path.read_text().splitlines()[-1])
    assert entry["outcome"] == "denied"


@pytest.mark.asyncio
async def test_tool_issues_token_on_first_call(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _enable(monkeypatch)
    target = tmp_path / "readme.txt"
    target.write_text("hello")
    out = await computer_fs_read_tool.handler(
        {"path": str(target)}, ctx,
    )
    # First call is NOT an error — it returns a token and a preview.
    assert not out.is_error
    assert out.data["needs_confirmation"] is True
    assert out.data["confirmation_token"]


@pytest.mark.asyncio
async def test_tool_executes_with_valid_token(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _enable(monkeypatch)
    target = tmp_path / "readme.txt"
    target.write_text("hello world")
    first = await computer_fs_read_tool.handler(
        {"path": str(target)}, ctx,
    )
    token = first.data["confirmation_token"]
    second = await computer_fs_read_tool.handler(
        {"path": str(target), "confirmation_token": token}, ctx,
    )
    assert not second.is_error
    assert "hello world" in second.content


@pytest.mark.asyncio
async def test_token_cannot_be_reused(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _enable(monkeypatch)
    target = tmp_path / "readme.txt"
    target.write_text("x")
    first = await computer_fs_read_tool.handler(
        {"path": str(target)}, ctx,
    )
    token = first.data["confirmation_token"]
    # First re-call consumes.
    await computer_fs_read_tool.handler(
        {"path": str(target), "confirmation_token": token}, ctx,
    )
    # Second re-call with the same token is rejected — but the tool
    # issues a fresh token so the agent can retry. Not is_error, but
    # needs_confirmation = True.
    third = await computer_fs_read_tool.handler(
        {"path": str(target), "confirmation_token": token}, ctx,
    )
    assert third.data.get("needs_confirmation") is True
    assert third.data["confirmation_token"] != token


@pytest.mark.asyncio
async def test_daily_limit_short_circuits(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _enable(monkeypatch)
    # Exhaust the limit (5 in the fixture).
    for _ in range(gate.daily_limit):
        gate.check_and_bump_daily()
    target = tmp_path / "x.txt"
    target.write_text("")
    # First call issues a token; the token consumer is where we hit
    # the daily limit.
    first = await computer_fs_read_tool.handler(
        {"path": str(target)}, ctx,
    )
    token = first.data["confirmation_token"]
    second = await computer_fs_read_tool.handler(
        {"path": str(target), "confirmation_token": token}, ctx,
    )
    assert second.is_error
    assert "Daily" in second.content or "daily" in second.content.lower()


# ── fs_read happy + error paths ────────────────────────────────


@pytest.mark.asyncio
async def test_fs_read_rejects_relative_path(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    first = await computer_fs_read_tool.handler(
        {"path": "relative/path.txt"}, ctx,
    )
    token = first.data["confirmation_token"]
    out = await computer_fs_read_tool.handler(
        {
            "path": "relative/path.txt",
            "confirmation_token": token,
        },
        ctx,
    )
    assert out.is_error
    assert "absolute" in out.content


@pytest.mark.asyncio
async def test_fs_read_truncates_large_files(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _enable(monkeypatch)
    huge = tmp_path / "huge.bin"
    huge.write_bytes(b"A" * (600 * 1024))  # > 512 KiB cap
    first = await computer_fs_read_tool.handler({"path": str(huge)}, ctx)
    token = first.data["confirmation_token"]
    out = await computer_fs_read_tool.handler(
        {"path": str(huge), "confirmation_token": token}, ctx,
    )
    assert not out.is_error
    assert out.data["truncated"] is True
    assert "[truncated" in out.content


# ── fs_write happy + error paths ───────────────────────────────


@pytest.mark.asyncio
async def test_fs_write_creates_file(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _enable(monkeypatch)
    target = tmp_path / "out.txt"
    args = {"path": str(target), "content": "hello"}
    first = await computer_fs_write_tool.handler(args, ctx)
    token = first.data["confirmation_token"]
    out = await computer_fs_write_tool.handler(
        {**args, "confirmation_token": token}, ctx,
    )
    assert not out.is_error
    assert target.read_text() == "hello"


@pytest.mark.asyncio
async def test_fs_write_rejects_over_2mib(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _enable(monkeypatch)
    target = tmp_path / "big.bin"
    args = {
        "path": str(target),
        "content": "A" * (3 * 1024 * 1024),
    }
    first = await computer_fs_write_tool.handler(args, ctx)
    token = first.data["confirmation_token"]
    out = await computer_fs_write_tool.handler(
        {**args, "confirmation_token": token}, ctx,
    )
    assert out.is_error
    assert "cap" in out.content


@pytest.mark.asyncio
async def test_fs_write_respects_hard_block(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _enable(monkeypatch)
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    target = fake_home / ".ssh" / "stolen"
    args = {"path": str(target), "content": "oops"}
    first = await computer_fs_write_tool.handler(args, ctx)
    token = first.data["confirmation_token"]
    out = await computer_fs_write_tool.handler(
        {**args, "confirmation_token": token}, ctx,
    )
    assert out.is_error
    assert "hard-blocked" in out.content


# ── shell happy + timeout ──────────────────────────────────────


@pytest.mark.asyncio
async def test_shell_runs_and_captures_output(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    args = {"command": "echo hello-pilk"}
    first = await computer_shell_tool.handler(args, ctx)
    token = first.data["confirmation_token"]
    out = await computer_shell_tool.handler(
        {**args, "confirmation_token": token}, ctx,
    )
    assert not out.is_error
    assert out.data["exit_code"] == 0
    assert "hello-pilk" in out.data["stdout"]


@pytest.mark.asyncio
async def test_shell_non_zero_exit_surfaces_error(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    args = {"command": "false"}
    first = await computer_shell_tool.handler(args, ctx)
    token = first.data["confirmation_token"]
    out = await computer_shell_tool.handler(
        {**args, "confirmation_token": token}, ctx,
    )
    assert out.is_error
    assert out.data["exit_code"] != 0


# ── osascript: unsupported-platform surfacing ──────────────────


@pytest.mark.asyncio
async def test_osascript_rejects_non_macos(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI runs on Linux; the tool must say 'only runs on macOS'
    and not pretend to work."""
    _enable(monkeypatch)
    args = {"script": "tell application \"Safari\" to activate"}
    first = await computer_osascript_tool.handler(args, ctx)
    token = first.data["confirmation_token"]
    out = await computer_osascript_tool.handler(
        {**args, "confirmation_token": token}, ctx,
    )
    # On macOS this would succeed; everywhere else it MUST fail clean.
    import platform as _p
    if _p.system() == "Darwin":
        pytest.skip("Running on macOS; can't test the cross-platform block.")
    assert out.is_error
    assert "macOS" in out.content


# ── audit log lands real outcomes ──────────────────────────────


@pytest.mark.asyncio
async def test_audit_log_records_ok_outcome(
    gate: ComputerControlGate, ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _enable(monkeypatch)
    target = tmp_path / "readme.txt"
    target.write_text("x")
    first = await computer_fs_read_tool.handler({"path": str(target)}, ctx)
    token = first.data["confirmation_token"]
    await computer_fs_read_tool.handler(
        {"path": str(target), "confirmation_token": token}, ctx,
    )
    lines = gate.audit_path.read_text().splitlines()
    last = json.loads(lines[-1])
    assert last["tool"] == "computer_fs_read"
    assert last["outcome"] == "ok"


# Helper so asyncio.run works inside a sync pytest session if we ever
# need to call a coroutine manually. Unused by the above tests (they
# use pytest-asyncio) but kept as a safety net.
def _run(co):
    return asyncio.run(co)


@contextlib.contextmanager
def _scoped_env(**kw: str) -> Iterator[None]:
    orig = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k, v in orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
