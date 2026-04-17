"""AgentSDKEngine — Anthropic Agent SDK inside a PILK sandbox.

Scaffold only. `available()` returns False until the SDK surface is
wired. The intent is: run a headless coding agent with the existing
sandboxed tools (fs_read, fs_write, shell_exec) — same billing pool as
the orchestrator's planner, just a different loop shape.
"""

from __future__ import annotations

from core.coding.base import CodeRunResult, CodeTask, EngineHealth


class AgentSDKEngine:
    name = "agent-sdk"
    label = "Anthropic Agent SDK"

    def __init__(self, enabled: bool = False) -> None:
        self._enabled = enabled

    async def available(self) -> bool:
        return False  # scaffolded only — wire in a follow-up batch

    async def health(self) -> EngineHealth:
        return EngineHealth(
            name=self.name,
            label=self.label,
            available=False,
            detail="scaffold only — wiring lands in a follow-up batch",
        )

    async def run(self, task: CodeTask) -> CodeRunResult:
        return CodeRunResult(
            engine=self.name,
            ok=False,
            summary=(
                "Agent SDK engine is scaffolded but not wired yet. "
                "Routing to another engine."
            ),
        )
