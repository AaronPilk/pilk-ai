"""Unit tests for CodexBridge. Mirrors test_coding_engines.py shape
for the Claude Code bridge; stubs `asyncio.create_subprocess_exec` so
no real CLI is invoked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.coding.base import CodeTask
from core.coding.codex_bridge import CodexBridge


class _FakeProc:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hang: bool = False,
        writes_file: tuple[str, str] | None = None,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self._writes_file = writes_file

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(60)
        if self._writes_file is not None:
            path, body = self._writes_file
            Path(path).write_text(body, encoding="utf-8")
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:  # pragma: no cover - best-effort
        pass


def _exec_factory(
    version: _FakeProc,
    run: _FakeProc | None = None,
    capture_output_file: bool = False,
):
    """Return a replacement for `asyncio.create_subprocess_exec` that
    hands back ``version`` on `--version` and ``run`` (or `version`)
    otherwise. If ``capture_output_file`` is True, the run proc's
    ``writes_file`` target is extracted from the argv."""

    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **_kwargs):
        calls.append(tuple(str(a) for a in args))
        if len(args) >= 2 and args[1] == "--version":
            return version
        if capture_output_file and run is not None and run._writes_file is None:
            # Find the --output-last-message path in the argv and make
            # the fake proc write the canned stdout into it.
            for i, a in enumerate(args):
                if str(a) == "--output-last-message" and i + 1 < len(args):
                    run._writes_file = (
                        str(args[i + 1]),
                        run._stdout.decode("utf-8", errors="replace"),
                    )
                    break
        return run or version

    fake_exec.calls = calls  # type: ignore[attr-defined]
    return fake_exec


@pytest.mark.asyncio
async def test_codex_unavailable_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    bridge = CodexBridge("codex")
    assert not await bridge.available()
    health = await bridge.health()
    assert not health.available
    assert "codex" in health.detail.lower()


@pytest.mark.asyncio
async def test_codex_available_when_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/codex")
    fake = _exec_factory(_FakeProc(returncode=0, stdout=b"0.28.0\n"))
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)
    bridge = CodexBridge("codex")
    assert await bridge.available()
    health = await bridge.health()
    assert health.available
    assert "/fake/bin/codex" in health.detail
    assert "full-auto" in health.detail


@pytest.mark.asyncio
async def test_codex_run_happy_path_reads_output_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/codex")
    fake = _exec_factory(
        _FakeProc(returncode=0, stdout=b"0.28.0"),
        _FakeProc(
            returncode=0,
            stdout=b"Refactored the module.\nAdded a regression test.",
        ),
        capture_output_file=True,
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)

    bridge = CodexBridge("codex")
    result = await bridge.run(
        CodeTask(goal="refactor it", scope="file", repo_path=tmp_path)
    )
    assert result.ok, result.summary
    assert "Refactored the module." in result.detail
    assert result.usd == 0.0

    run_argv = next(c for c in fake.calls if "exec" in c)
    # Expected flags in the argv shape
    assert "--full-auto" in run_argv
    assert "--ephemeral" in run_argv
    assert "--cd" in run_argv
    assert str(tmp_path) in run_argv
    assert "--output-last-message" in run_argv
    assert run_argv[-1] == "refactor it"


@pytest.mark.asyncio
async def test_codex_yolo_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/codex")
    fake = _exec_factory(
        _FakeProc(returncode=0, stdout=b"0.28.0"),
        _FakeProc(returncode=0, stdout=b"done"),
        capture_output_file=True,
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)
    bridge = CodexBridge("codex", yolo=True)
    await bridge.run(CodeTask(goal="go", scope="function", repo_path=tmp_path))
    run_argv = next(c for c in fake.calls if "exec" in c)
    assert "--dangerously-bypass-approvals-and-sandbox" in run_argv
    # YOLO disables the default --full-auto
    assert "--full-auto" not in run_argv


@pytest.mark.asyncio
async def test_codex_sandbox_mode_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/codex")
    fake = _exec_factory(
        _FakeProc(returncode=0, stdout=b"0.28.0"),
        _FakeProc(returncode=0, stdout=b"done"),
        capture_output_file=True,
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)
    bridge = CodexBridge("codex", sandbox_mode="read-only")
    await bridge.run(CodeTask(goal="analyze", scope="file", repo_path=tmp_path))
    run_argv = next(c for c in fake.calls if "exec" in c)
    assert "--sandbox" in run_argv
    assert "read-only" in run_argv
    assert "--full-auto" not in run_argv


@pytest.mark.asyncio
async def test_codex_model_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/codex")
    fake = _exec_factory(
        _FakeProc(returncode=0, stdout=b"0.28.0"),
        _FakeProc(returncode=0, stdout=b"done"),
        capture_output_file=True,
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)
    bridge = CodexBridge("codex", model="gpt-5-codex-preview")
    await bridge.run(CodeTask(goal="go", scope="function", repo_path=tmp_path))
    run_argv = next(c for c in fake.calls if "exec" in c)
    assert "--model" in run_argv
    assert "gpt-5-codex-preview" in run_argv


@pytest.mark.asyncio
async def test_codex_run_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/codex")
    fake = _exec_factory(
        _FakeProc(returncode=0, stdout=b"0.28.0"),
        _FakeProc(returncode=2, stderr=b"login required"),
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)
    bridge = CodexBridge("codex")
    result = await bridge.run(
        CodeTask(goal="x", scope="file", repo_path=tmp_path)
    )
    assert not result.ok
    assert "exited 2" in result.summary
    assert "login required" in result.detail


@pytest.mark.asyncio
async def test_codex_run_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/codex")
    fake = _exec_factory(
        _FakeProc(returncode=0, stdout=b"0.28.0"),
        _FakeProc(returncode=0, hang=True),
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)
    monkeypatch.setattr("core.coding.codex_bridge.RUN_TIMEOUT_S", 0.05)
    bridge = CodexBridge("codex")
    result = await bridge.run(
        CodeTask(goal="slow", scope="file", repo_path=tmp_path)
    )
    assert not result.ok
    assert "timed out" in result.summary.lower()
