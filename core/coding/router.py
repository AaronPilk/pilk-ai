"""CodingRouter — chooses which engine handles a given CodeTask.

Decision order:

  1. Explicit `task.prefer_engine` → use it if healthy, fall through
     otherwise.
  2. `task.scope == "repo"`  → prefer ClaudeCodeBridge, then AgentSDKEngine.
     Both are skipped if the Governor reports the daily budget is over
     (Claude Code runs are free, but an over-budget state is usually a
     signal to cool down anywhere we can).
  3. `task.scope in ("function", "file")` → APIEngine.
  4. Fallback → the first engine that reports available.

The router is stateless; engines are the only place that hold state.
"""

from __future__ import annotations

from typing import Any

from core.coding.base import CodeTask, CodingEngine
from core.logging import get_logger

log = get_logger("pilkd.coding.router")


class CodingRouter:
    def __init__(
        self,
        engines: dict[str, CodingEngine],
        governor: Any = None,
    ) -> None:
        self._engines = dict(engines)
        self._governor = governor

    def names(self) -> list[str]:
        return list(self._engines.keys())

    def get(self, name: str) -> CodingEngine | None:
        return self._engines.get(name)

    async def pick(self, task: CodeTask) -> CodingEngine | None:
        # 1. explicit override
        if task.prefer_engine:
            engine = self._engines.get(task.prefer_engine)
            if engine is not None and await engine.available():
                return engine
            log.info(
                "coding_router_override_unavailable",
                requested=task.prefer_engine,
                falling_back=True,
            )

        over_budget = self._over_budget()

        # 2. repo scope → Claude Code, then Agent SDK
        if task.scope == "repo":
            for name in ("claude-code", "agent-sdk"):
                engine = self._engines.get(name)
                if engine is None:
                    continue
                if not await engine.available():
                    continue
                if over_budget and name != "claude-code":
                    # Non-subscription engines skipped when budget is tight.
                    continue
                return engine

        # 3. function/file scope → API engine
        if task.scope in ("function", "file"):
            engine = self._engines.get("api")
            if engine is not None and await engine.available():
                return engine

        # 4. last resort — first available engine
        for engine in self._engines.values():
            if await engine.available():
                return engine
        return None

    def _over_budget(self) -> bool:
        g = self._governor
        if g is None:
            return False
        budget = getattr(g, "budget", None)
        return bool(getattr(budget, "is_over", False))
