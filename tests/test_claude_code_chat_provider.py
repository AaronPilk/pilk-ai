"""Unit tests for the subscription-backed Claude Code chat provider.

The provider shells out to the ``claude`` CLI in ``-p --output-format
json`` mode so the operator's Max/Pro subscription covers chat turns
at $0 marginal cost (vs the API path that bills token-by-token).

These tests stub ``asyncio.create_subprocess_exec`` so we can assert:

- init resolves / refuses the binary correctly
- argv is built with the flags the CLI actually expects
- the CLI's JSON envelope is parsed into a text-only PlannerResponse
- messages are flattened + oldest entries dropped under the char cap
- tools are silently stripped (logged but not forwarded)
- timeouts + non-zero exits surface as RuntimeError so the orchestrator
  can fall back to the API path
- `build_providers` registers the provider when the binary is
  available and skips it gracefully when it isn't
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.governor.providers import build_providers
from core.governor.providers.claude_code_provider import (
    HISTORY_CHAR_BUDGET,
    PILK_APPEND_PROMPT,
    ClaudeCodeBinaryMissingError,
    ClaudeCodeChatProvider,
)

# ── Subprocess stubbing ─────────────────────────────────────────


class _FakeProcess:
    """Stand-in for an asyncio subprocess. Whatever stdout/stderr/
    returncode the test seeds is what `communicate` reports."""

    def __init__(
        self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


class _SpawnSpy:
    """Records every create_subprocess_exec call + returns a canned
    process. Swap via monkeypatch before each test."""

    def __init__(self, proc: _FakeProcess) -> None:
        self._proc = proc
        self.calls: list[list[str]] = []

    async def __call__(self, *argv: str, **_kw: Any) -> _FakeProcess:
        self.calls.append(list(argv))
        return self._proc


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch, proc: _FakeProcess,
) -> _SpawnSpy:
    spy = _SpawnSpy(proc)
    import core.governor.providers.claude_code_provider as mod
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", spy)
    return spy


def _pretend_binary_exists(
    monkeypatch: pytest.MonkeyPatch, path: str = "/usr/local/bin/claude",
) -> None:
    """Bypass the PATH check so tests don't depend on a real
    install."""
    import core.governor.providers.claude_code_provider as mod
    monkeypatch.setattr(
        mod.ClaudeCodeChatProvider, "_resolve_binary",
        staticmethod(lambda _c: path),
    )


# ── init ────────────────────────────────────────────────────────


def test_init_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.governor.providers.claude_code_provider as mod
    monkeypatch.setattr(
        mod.ClaudeCodeChatProvider, "_resolve_binary",
        staticmethod(lambda _c: None),
    )
    with pytest.raises(ClaudeCodeBinaryMissingError):
        ClaudeCodeChatProvider()


def test_init_accepts_resolved_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_binary_exists(monkeypatch, "/opt/bin/claude")
    p = ClaudeCodeChatProvider(binary="claude")
    assert p._binary == "/opt/bin/claude"
    assert p._max_turns >= 1


# ── argv construction ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_argv_contains_required_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_binary_exists(monkeypatch)
    spy = _patch_subprocess(
        monkeypatch,
        _FakeProcess(stdout=json.dumps({"result": "hi"}).encode()),
    )
    p = ClaudeCodeChatProvider()
    await p.plan_turn(
        system="You are PILK.",
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        model="claude-haiku-4-5",
        max_tokens=1024,
        use_thinking=False,
        cache_control=True,
    )
    assert len(spy.calls) == 1
    argv = spy.calls[0]
    # Core CLI surface used by the existing ClaudeCodeBridge — if
    # the CLI loses any of these flags we want this test to fail
    # loudly rather than silently bill the operator for API calls.
    assert argv[0].endswith("claude")
    assert "-p" in argv
    assert "--output-format" in argv and "json" in argv
    assert "--bare" in argv
    assert "--no-session-persistence" in argv
    assert "--max-turns" in argv
    assert "--permission-mode" in argv


@pytest.mark.asyncio
async def test_argv_includes_model_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_binary_exists(monkeypatch)
    spy = _patch_subprocess(
        monkeypatch,
        _FakeProcess(stdout=json.dumps({"result": "ok"}).encode()),
    )
    p = ClaudeCodeChatProvider()
    await p.plan_turn(
        system="", messages=[{"role": "user", "content": "hi"}],
        tools=[], model="claude-sonnet-4-6",
        max_tokens=1024, use_thinking=False, cache_control=False,
    )
    argv = spy.calls[0]
    assert "--model" in argv
    assert "claude-sonnet-4-6" in argv


@pytest.mark.asyncio
async def test_argv_sends_pilk_preamble_with_operator_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI sees our PILK preamble + the orchestrator's system
    prompt concatenated. Losing the preamble means the CLI starts
    thinking it's a free-form coding session."""
    _pretend_binary_exists(monkeypatch)
    spy = _patch_subprocess(
        monkeypatch,
        _FakeProcess(stdout=json.dumps({"result": "ok"}).encode()),
    )
    p = ClaudeCodeChatProvider()
    await p.plan_turn(
        system="Operator-side PILK prompt.",
        messages=[{"role": "user", "content": "hi"}],
        tools=[], model="", max_tokens=1024,
        use_thinking=False, cache_control=True,
    )
    argv = spy.calls[0]
    idx = argv.index("--append-system-prompt")
    preamble = argv[idx + 1]
    assert PILK_APPEND_PROMPT.split(".")[0] in preamble
    assert "Operator-side PILK prompt." in preamble


@pytest.mark.asyncio
async def test_argv_places_user_prompt_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI reads the final positional arg as the user prompt —
    flag ordering matters."""
    _pretend_binary_exists(monkeypatch)
    spy = _patch_subprocess(
        monkeypatch,
        _FakeProcess(stdout=json.dumps({"result": "ok"}).encode()),
    )
    p = ClaudeCodeChatProvider()
    await p.plan_turn(
        system="", messages=[{"role": "user", "content": "the question"}],
        tools=[], model="", max_tokens=1024,
        use_thinking=False, cache_control=False,
    )
    argv = spy.calls[0]
    assert argv[-1].endswith("the question")


# ── JSON parsing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parses_result_field_from_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_binary_exists(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(
            stdout=json.dumps({
                "result": "Hi, I'm PILK.",
                "model": "claude-sonnet-4-6",
                "session_id": "abc",
                "total_cost_usd": 0.0,
            }).encode(),
        ),
    )
    p = ClaudeCodeChatProvider()
    resp = await p.plan_turn(
        system="", messages=[{"role": "user", "content": "hi"}],
        tools=[], model="claude-haiku-4-5",
        max_tokens=1024, use_thinking=False, cache_control=False,
    )
    assert len(resp.content) == 1
    assert resp.content[0].text == "Hi, I'm PILK."
    # Provider surfaces the model the CLI actually used — useful when
    # the subscription overrides the caller's requested model.
    assert resp.model == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_parses_nested_content_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older / beta CLI schemas put text under message.content
    blocks. Support that too."""
    _pretend_binary_exists(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(
            stdout=json.dumps({
                "message": {
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "text", "text": " world"},
                    ],
                },
            }).encode(),
        ),
    )
    p = ClaudeCodeChatProvider()
    resp = await p.plan_turn(
        system="", messages=[{"role": "user", "content": "x"}],
        tools=[], model="", max_tokens=1024,
        use_thinking=False, cache_control=False,
    )
    assert resp.content[0].text == "hello\n world"


@pytest.mark.asyncio
async def test_non_json_output_falls_back_to_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_binary_exists(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(stdout=b"just plain text\n"),
    )
    p = ClaudeCodeChatProvider()
    resp = await p.plan_turn(
        system="", messages=[{"role": "user", "content": "x"}],
        tools=[], model="", max_tokens=1024,
        use_thinking=False, cache_control=False,
    )
    assert resp.content[0].text == "just plain text"


@pytest.mark.asyncio
async def test_usage_reports_zero_tokens_for_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscription-backed CLI calls don't report per-token usage —
    the provider surfaces zeros so the cost ledger doesn't bill the
    operator twice (subscription + API)."""
    _pretend_binary_exists(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(stdout=json.dumps({"result": "hi"}).encode()),
    )
    p = ClaudeCodeChatProvider()
    resp = await p.plan_turn(
        system="", messages=[{"role": "user", "content": "x"}],
        tools=[], model="", max_tokens=1024,
        use_thinking=False, cache_control=False,
    )
    assert resp.usage.input_tokens == 0
    assert resp.usage.output_tokens == 0


# ── flattening messages ────────────────────────────────────────


def test_flatten_joins_roles_and_text() -> None:
    out = ClaudeCodeChatProvider._flatten([
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "what can you do?"},
    ])
    assert "user: hey" in out
    assert "assistant: hi" in out
    assert out.endswith("user: what can you do?")


def test_flatten_handles_block_list_content() -> None:
    out = ClaudeCodeChatProvider._flatten([
        {"role": "user", "content": [
            {"type": "text", "text": "part1"},
            {"type": "text", "text": "part2"},
        ]},
    ])
    assert "part1" in out
    assert "part2" in out


def test_flatten_drops_oldest_under_char_budget() -> None:
    """When history exceeds the char budget, the latest exchange
    must still land at the bottom."""
    big = "x" * (HISTORY_CHAR_BUDGET // 4)
    msgs = [
        {"role": "user", "content": big},
        {"role": "assistant", "content": big},
        {"role": "user", "content": big},
        {"role": "assistant", "content": big},
        {"role": "user", "content": big},
        {"role": "assistant", "content": "FINAL REPLY"},
    ]
    out = ClaudeCodeChatProvider._flatten(msgs)
    # Latest exchange preserved, older ones trimmed.
    assert out.endswith("FINAL REPLY")
    assert len(out) <= HISTORY_CHAR_BUDGET + 40  # small slack for prefixes


def test_flatten_empty_returns_empty_string() -> None:
    assert ClaudeCodeChatProvider._flatten([]) == ""


# ── tools are stripped silently ────────────────────────────────


@pytest.mark.asyncio
async def test_tools_are_stripped_not_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Our custom tools aren't known to the CLI. Forwarding them
    would pollute argv or break the CLI entirely. The provider must
    silently strip them and let the model reply in plain text."""
    _pretend_binary_exists(monkeypatch)
    spy = _patch_subprocess(
        monkeypatch,
        _FakeProcess(stdout=json.dumps({"result": "ok"}).encode()),
    )
    p = ClaudeCodeChatProvider()
    await p.plan_turn(
        system="", messages=[{"role": "user", "content": "x"}],
        tools=[
            {"name": "fs_read", "description": "read", "input_schema": {}},
            {"name": "fs_write", "description": "write", "input_schema": {}},
        ],
        model="", max_tokens=1024,
        use_thinking=False, cache_control=False,
    )
    argv = spy.calls[0]
    # No tool name should leak into argv. The CLI should also not
    # receive a --tools flag (we don't use one).
    joined = " ".join(argv)
    assert "fs_read" not in joined
    assert "fs_write" not in joined


# ── failure modes bubble up as RuntimeError ───────────────────


@pytest.mark.asyncio
async def test_non_zero_exit_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_binary_exists(monkeypatch)
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(
            stdout=b"", stderr=b"subscription quota exhausted",
            returncode=1,
        ),
    )
    p = ClaudeCodeChatProvider()
    with pytest.raises(RuntimeError) as exc:
        await p.plan_turn(
            system="", messages=[{"role": "user", "content": "x"}],
            tools=[], model="", max_tokens=1024,
            use_thinking=False, cache_control=False,
        )
    assert "subscription quota exhausted" in str(exc.value)


@pytest.mark.asyncio
async def test_timeout_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung subprocess must not hang the chat turn forever. After
    the timeout we kill the process and raise so the orchestrator
    can fall back to the API path."""
    import asyncio as _asyncio

    class _HangProc(_FakeProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            await _asyncio.sleep(10)  # longer than the 5s timeout
            return b"", b""

    _pretend_binary_exists(monkeypatch)
    _patch_subprocess(monkeypatch, _HangProc())
    p = ClaudeCodeChatProvider(timeout_s=1)
    with pytest.raises(RuntimeError) as exc:
        await p.plan_turn(
            system="", messages=[{"role": "user", "content": "x"}],
            tools=[], model="", max_tokens=1024,
            use_thinking=False, cache_control=False,
        )
    assert "timed out" in str(exc.value)


# ── build_providers registration ───────────────────────────────


def test_build_providers_registers_when_binary_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_binary_exists(monkeypatch)
    providers = build_providers(
        anthropic_client=None,
        openai_api_key=None,
        enable_claude_code_chat=True,
    )
    assert "claude_code" in providers


def test_build_providers_skips_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.governor.providers.claude_code_provider as mod
    monkeypatch.setattr(
        mod.ClaudeCodeChatProvider, "_resolve_binary",
        staticmethod(lambda _c: None),
    )
    providers = build_providers(
        anthropic_client=None,
        openai_api_key=None,
        enable_claude_code_chat=True,
    )
    assert "claude_code" not in providers


def test_build_providers_skips_when_feature_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the binary present, the master switch off means
    the provider isn't registered — every call lands on the API."""
    _pretend_binary_exists(monkeypatch)
    providers = build_providers(
        anthropic_client=None,
        openai_api_key=None,
        enable_claude_code_chat=False,
    )
    assert "claude_code" not in providers
