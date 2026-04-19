"""Unit tests for the two newly-wired coding engines.

Covers:

* ``ClaudeCodeBridge`` — binary resolution, probe success/failure,
  subprocess run with captured stdout, non-zero exit surface, and
  timeout handling. No real `claude` CLI is invoked; we stub
  ``asyncio.create_subprocess_exec`` to return a controllable fake.

* ``AgentSDKEngine`` — unavailability when no client, happy-path
  single-turn answer, happy-path tool-use round trip (fs_read called
  with the model's chosen path), turn-cap surfacing, and clean error
  propagation when the Anthropic client raises.

All offline; no network, no real subprocess.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from core.coding.agent_sdk import AgentSDKEngine
from core.coding.base import CodeTask
from core.coding.claude_code_bridge import ClaudeCodeBridge

# ── Shared stubs ────────────────────────────────────────────────


class _FakeProc:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        hang: bool = False,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self._killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(60)  # longer than any test timeout
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self._killed = True


def _exec_factory(
    which_version: _FakeProc,
    which_run: _FakeProc | None = None,
):
    """Returns an `asyncio.create_subprocess_exec` replacement that
    hands back ``which_version`` for the probe and ``which_run``
    (falling back to the probe value) for the actual run."""

    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **_kwargs):
        calls.append(tuple(str(a) for a in args))
        if len(args) >= 2 and args[1] == "--version":
            return which_version
        return which_run or which_version

    fake_exec.calls = calls  # type: ignore[attr-defined]
    return fake_exec


# ── ClaudeCodeBridge ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claude_code_unavailable_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    bridge = ClaudeCodeBridge("nope-not-a-real-bin")
    assert not await bridge.available()
    health = await bridge.health()
    assert not health.available
    assert "install" in health.detail.lower()


@pytest.mark.asyncio
async def test_claude_code_available_when_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/claude")
    fake = _exec_factory(_FakeProc(returncode=0, stdout=b"2.0.0\n"))
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)
    bridge = ClaudeCodeBridge("claude")
    assert await bridge.available()
    health = await bridge.health()
    assert health.available
    assert "/fake/bin/claude" in health.detail


@pytest.mark.asyncio
async def test_claude_code_unavailable_when_probe_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/claude")
    fake = _exec_factory(_FakeProc(returncode=127))
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)
    bridge = ClaudeCodeBridge("claude")
    assert not await bridge.available()


@pytest.mark.asyncio
async def test_claude_code_run_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/claude")
    version_proc = _FakeProc(returncode=0, stdout=b"2.0.0\n")
    run_proc = _FakeProc(
        returncode=0,
        stdout=b"First line of Claude's answer.\nA second line with detail.\n",
    )
    fake = _exec_factory(version_proc, run_proc)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)

    bridge = ClaudeCodeBridge("claude")
    task = CodeTask(goal="refactor main.py", scope="file", repo_path=tmp_path)
    result = await bridge.run(task)

    assert result.ok
    assert "First line of Claude's answer." in result.summary
    assert "A second line with detail." in result.detail
    assert result.usd == 0.0  # subscription-billed
    # At least one run call was made with --print
    run_args = [c for c in fake.calls if "--print" in c]
    assert run_args, "expected a --print invocation"
    assert "refactor main.py" in run_args[0]


@pytest.mark.asyncio
async def test_claude_code_run_nonzero_exit_surfaces_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/claude")
    fake = _exec_factory(
        _FakeProc(returncode=0, stdout=b"2.0.0"),
        _FakeProc(returncode=3, stderr=b"auth required: run `claude login`"),
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)
    bridge = ClaudeCodeBridge("claude")
    result = await bridge.run(
        CodeTask(goal="do a thing", scope="file", repo_path=tmp_path)
    )
    assert not result.ok
    assert "exited 3" in result.summary
    assert "auth required" in result.detail


@pytest.mark.asyncio
async def test_claude_code_run_timeout_kills_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/bin/claude")
    # Probe succeeds; run hangs.
    fake = _exec_factory(
        _FakeProc(returncode=0, stdout=b"2.0.0"),
        _FakeProc(returncode=0, hang=True),
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake)
    monkeypatch.setattr(
        "core.coding.claude_code_bridge.RUN_TIMEOUT_S", 0.05
    )
    bridge = ClaudeCodeBridge("claude")
    result = await bridge.run(
        CodeTask(goal="slow job", scope="file", repo_path=tmp_path)
    )
    assert not result.ok
    assert "timed out" in result.summary.lower()


# ── AgentSDKEngine ──────────────────────────────────────────────


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


class _FakeResponse:
    def __init__(self, content: list[Any], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _ScriptedMessages:
    def __init__(self, client: _ScriptedClient) -> None:
        self._client = client

    async def create(self, **kwargs):
        self._client.calls.append(kwargs)
        return self._client._responses.pop(0)


class _ScriptedClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.messages = _ScriptedMessages(self)


@pytest.mark.asyncio
async def test_agent_sdk_unavailable_without_client() -> None:
    engine = AgentSDKEngine(client=None, model="claude-haiku-4-5")
    assert not await engine.available()
    health = await engine.health()
    assert not health.available
    assert "ANTHROPIC_API_KEY" in health.detail


@pytest.mark.asyncio
async def test_agent_sdk_single_turn_final_text() -> None:
    client = _ScriptedClient(
        [
            _FakeResponse(
                content=[_TextBlock(text="Here's a short answer.")],
                stop_reason="end_turn",
            )
        ]
    )
    engine = AgentSDKEngine(client=client, model="claude-haiku-4-5")
    result = await engine.run(CodeTask(goal="write a hello fn", scope="function"))
    assert result.ok
    assert result.summary.startswith("Drafted")
    assert "Here's a short answer." in result.detail
    # A single planner call was made.
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_agent_sdk_tool_use_loop_invokes_fs_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the model emits a tool_use, the engine must execute it via
    fs_read and feed the tool_result back for a second turn."""

    # Point fs_read at an isolated root so we can assert the read.
    from core.config import get_settings
    from core.tools.builtin import fs as fs_mod

    target = tmp_path / "hello.py"
    target.write_text("def hello():\n    return 'world'\n")
    monkeypatch.setattr(fs_mod, "_root_for", lambda _ctx: tmp_path)
    get_settings.cache_clear()

    client = _ScriptedClient(
        [
            _FakeResponse(
                content=[
                    _ToolUseBlock(
                        id="tu1",
                        name="fs_read",
                        input={"path": "hello.py"},
                    )
                ],
                stop_reason="tool_use",
            ),
            _FakeResponse(
                content=[_TextBlock(text="File returns 'world'.")],
                stop_reason="end_turn",
            ),
        ]
    )
    engine = AgentSDKEngine(client=client, model="claude-haiku-4-5")
    result = await engine.run(
        CodeTask(goal="what does hello.py do?", scope="file")
    )
    assert result.ok, result.summary
    assert "File returns 'world'." in result.detail
    # Second call messages include the assistant tool_use block +
    # the user-role tool_result block with the file body.
    second_call_messages = client.calls[1]["messages"]
    assert second_call_messages[-1]["role"] == "user"
    tool_result = second_call_messages[-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "tu1"
    assert "world" in tool_result["content"]


@pytest.mark.asyncio
async def test_agent_sdk_hits_turn_cap_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.tools.builtin import fs as fs_mod

    monkeypatch.setattr(fs_mod, "_root_for", lambda _ctx: tmp_path)
    (tmp_path / "x").write_text("x")
    monkeypatch.setattr("core.coding.agent_sdk.MAX_TURNS", 2)

    client = _ScriptedClient(
        [
            _FakeResponse(
                content=[_ToolUseBlock(id="a", name="fs_read", input={"path": "x"})],
                stop_reason="tool_use",
            ),
            _FakeResponse(
                content=[_ToolUseBlock(id="b", name="fs_read", input={"path": "x"})],
                stop_reason="tool_use",
            ),
        ]
    )
    engine = AgentSDKEngine(client=client, model="claude-haiku-4-5")
    result = await engine.run(CodeTask(goal="loop forever", scope="file"))
    # No final text ever landed — the engine should return ok=False.
    assert not result.ok
    assert "no final text" in result.summary.lower()


@pytest.mark.asyncio
async def test_agent_sdk_surfaces_anthropic_exception() -> None:
    class _Boom:
        class messages:  # noqa: N801 - mirror SDK shape
            @staticmethod
            async def create(**_kwargs):
                raise RuntimeError("429 rate limited")

    engine = AgentSDKEngine(client=_Boom(), model="claude-haiku-4-5")
    result = await engine.run(CodeTask(goal="please fail", scope="function"))
    assert not result.ok
    assert "429" in result.summary
