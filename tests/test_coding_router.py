"""CodingRouter picks the right engine per task + governor state."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from core.coding import CodingEngine, CodingRouter
from core.coding.base import CodeRunResult, CodeTask, EngineHealth


class _FakeEngine:
    def __init__(self, name: str, label: str, ok: bool) -> None:
        self.name = name
        self.label = label
        self._ok = ok
        self.calls = 0

    async def available(self) -> bool:
        return self._ok

    async def health(self) -> EngineHealth:
        return EngineHealth(name=self.name, label=self.label, available=self._ok)

    async def run(self, task: CodeTask) -> CodeRunResult:
        self.calls += 1
        return CodeRunResult(engine=self.name, ok=True, summary=f"ran on {self.name}")


assert isinstance(_FakeEngine("x", "X", True), CodingEngine)  # protocol sanity


@dataclass
class _Budget:
    is_over: bool = False


class _Governor:
    def __init__(self, over: bool = False) -> None:
        self.budget = _Budget(is_over=over)


@pytest.mark.asyncio
async def test_repo_scope_prefers_claude_code_when_healthy() -> None:
    cc = _FakeEngine("claude-code", "Claude Code", ok=True)
    sdk = _FakeEngine("agent-sdk", "Agent SDK", ok=True)
    api = _FakeEngine("api", "API", ok=True)
    router = CodingRouter(
        {"claude-code": cc, "agent-sdk": sdk, "api": api}, governor=_Governor()
    )
    chosen = await router.pick(CodeTask(goal="refactor the repo", scope="repo"))
    assert chosen is cc


@pytest.mark.asyncio
async def test_repo_scope_falls_back_when_bridge_down() -> None:
    cc = _FakeEngine("claude-code", "Claude Code", ok=False)
    sdk = _FakeEngine("agent-sdk", "Agent SDK", ok=True)
    api = _FakeEngine("api", "API", ok=True)
    router = CodingRouter(
        {"claude-code": cc, "agent-sdk": sdk, "api": api}, governor=_Governor()
    )
    chosen = await router.pick(CodeTask(goal="refactor", scope="repo"))
    assert chosen is sdk


@pytest.mark.asyncio
async def test_function_scope_goes_to_api_even_with_bridge_up() -> None:
    cc = _FakeEngine("claude-code", "Claude Code", ok=True)
    api = _FakeEngine("api", "API", ok=True)
    router = CodingRouter(
        {"claude-code": cc, "api": api}, governor=_Governor()
    )
    chosen = await router.pick(CodeTask(goal="tiny helper", scope="function"))
    assert chosen is api


@pytest.mark.asyncio
async def test_over_budget_still_allows_subscription_engine() -> None:
    cc = _FakeEngine("claude-code", "Claude Code", ok=True)
    sdk = _FakeEngine("agent-sdk", "Agent SDK", ok=True)
    api = _FakeEngine("api", "API", ok=True)
    router = CodingRouter(
        {"claude-code": cc, "agent-sdk": sdk, "api": api},
        governor=_Governor(over=True),
    )
    chosen = await router.pick(CodeTask(goal="repo work", scope="repo"))
    # Claude Code (free/subscription) stays eligible; agent-sdk would be
    # skipped if Claude Code was down.
    assert chosen is cc


@pytest.mark.asyncio
async def test_over_budget_skips_agent_sdk_in_repo_scope_path() -> None:
    cc = _FakeEngine("claude-code", "Claude Code", ok=False)
    sdk = _FakeEngine("agent-sdk", "Agent SDK", ok=True)
    api = _FakeEngine("api", "API", ok=True)
    router = CodingRouter(
        {"claude-code": cc, "agent-sdk": sdk, "api": api},
        governor=_Governor(over=True),
    )
    # SDK is skipped inside the repo-scope preference block under budget
    # pressure; we fall through to the last-resort loop, which picks the
    # first available engine — SDK again, because it's healthy. That's
    # intentional: refusing to run is worse than running on SDK once the
    # repo-specific preference path has been exhausted.
    chosen = await router.pick(CodeTask(goal="repo work", scope="repo"))
    assert chosen is sdk


@pytest.mark.asyncio
async def test_explicit_prefer_wins_when_available() -> None:
    cc = _FakeEngine("claude-code", "Claude Code", ok=True)
    api = _FakeEngine("api", "API", ok=True)
    router = CodingRouter(
        {"claude-code": cc, "api": api}, governor=_Governor()
    )
    chosen = await router.pick(
        CodeTask(goal="anything", scope="function", prefer_engine="claude-code")
    )
    assert chosen is cc


@pytest.mark.asyncio
async def test_no_available_engine_returns_none() -> None:
    cc = _FakeEngine("claude-code", "Claude Code", ok=False)
    api = _FakeEngine("api", "API", ok=False)
    router = CodingRouter({"claude-code": cc, "api": api}, governor=_Governor())
    chosen = await router.pick(CodeTask(goal="x", scope="function"))
    assert chosen is None
