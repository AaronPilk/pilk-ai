"""ClaudeCodeBridge — delegate repo-scope work to a local Claude Code session.

Scaffold only. `available()` returns True only when the env var
`PILK_CLAUDE_CODE_BRIDGE_URL` is set *and* a small health probe
succeeds. Today no real protocol is defined; `run()` refuses until a
follow-up batch lands the wire format.

When the bridge is wired, responsibility model:
- PILK holds a single approval for the delegated task ("delegate
  coding task to Claude Code: <goal>"); Claude Code's own permission
  UI handles fine-grained approvals inside the run.
- Billing is separate: runs record `cost_kind="claude-code"` and
  `usd=0.0` so the Anthropic API daily cap is not polluted.
"""

from __future__ import annotations

import asyncio

from core.coding.base import CodeRunResult, CodeTask, EngineHealth
from core.logging import get_logger

log = get_logger("pilkd.coding.claude_code")


class ClaudeCodeBridge:
    name = "claude-code"
    label = "Claude Code (local)"

    def __init__(self, bridge_url: str | None) -> None:
        self._bridge_url = (bridge_url or "").strip() or None

    async def available(self) -> bool:
        return self._bridge_url is not None and await self._probe()

    async def health(self) -> EngineHealth:
        if self._bridge_url is None:
            return EngineHealth(
                name=self.name,
                label=self.label,
                available=False,
                detail="set PILK_CLAUDE_CODE_BRIDGE_URL to enable",
            )
        if not await self._probe():
            return EngineHealth(
                name=self.name,
                label=self.label,
                available=False,
                detail=f"bridge at {self._bridge_url} not responding",
            )
        return EngineHealth(
            name=self.name,
            label=self.label,
            available=True,
            detail=f"bridge: {self._bridge_url}",
        )

    async def run(self, task: CodeTask) -> CodeRunResult:
        # Bridge protocol not defined yet. When added, this method will
        # POST the task to the bridge and stream the result back with
        # a single PILK-side approval covering the whole delegated run.
        return CodeRunResult(
            engine=self.name,
            ok=False,
            summary=(
                "Claude Code bridge is scaffolded but not wired yet. "
                "Routing to another engine."
            ),
        )

    async def _probe(self) -> bool:
        # Placeholder probe. The eventual implementation does a cheap
        # request (HTTP GET /health or a socket ping) with a short
        # timeout. Today: no outbound calls; just report the env var.
        if self._bridge_url is None:
            return False
        await asyncio.sleep(0)  # keep the coroutine shape honest
        return False
