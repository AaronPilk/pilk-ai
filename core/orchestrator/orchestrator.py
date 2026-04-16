"""Orchestrator: turns a user goal into a plan and drives the tool loop.

One Claude tool-use loop per goal. We own the loop (not the SDK's tool
runner) so every turn can:
  - create/update a step in the plan store,
  - record LLM usage against the cost ledger (including cache tokens),
  - gate every tool call through the gateway (risk + policy),
  - broadcast live events to connected dashboards.

Two entry points share the loop:
  - `run(goal)` — free chat, no agent, scoped to the shared workspace.
  - `agent_run(name, task)` — runs through a registered agent with its
    manifest's system prompt, tool subset, and sandbox.

Model: Opus 4.7 with adaptive thinking (no budget_tokens, no sampling
params — both are rejected on 4.7). Prompt caching is applied at the
top level; tools and system are stable and get the cache hit, messages
grow per turn. Max turns bounded by settings.plan_max_turns.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic

from core.ledger import Ledger, UsageSnapshot
from core.logging import get_logger
from core.orchestrator.plans import PlanStore
from core.registry import AgentRegistry
from core.sandbox import SandboxManager
from core.tools import Gateway, ToolRegistry
from core.tools.registry import ToolContext

log = get_logger("pilkd.orchestrator")

Broadcaster = Callable[[str, dict[str, Any]], Awaitable[None]]

DEFAULT_SYSTEM_PROMPT = """You are PILK, a personal execution operating system.
You receive a user goal, build a plan, and execute it by calling the tools
available to you. You run locally on the user's laptop.

Rules of engagement:
- Prefer the cheapest adequate action. Read files before editing them.
  Use shell_exec only when a dedicated tool won't do. Use llm_ask for
  bounded sub-tasks where a smaller model suffices.
- All filesystem and shell work is scoped to your working directory. If a
  tool refuses a path, don't retry with a different absolute path — it
  will also be refused.
- When a task is complete, finish with a short summary of what you did
  and where the results live. Do not chain extra speculative work.
- Be concise. The user sees your text responses directly in a chat pane.
"""


class OrchestratorBusyError(RuntimeError):
    """Raised when a second plan is submitted while one is running."""


@dataclass
class RunContext:
    goal: str
    system_prompt: str
    allowed_tools: list[str] | None  # None = all registered tools
    agent_name: str | None
    sandbox_id: str | None
    sandbox_root: Path | None
    sandbox_capabilities: frozenset[str]
    metadata: dict[str, Any]


class Orchestrator:
    def __init__(
        self,
        *,
        client: anthropic.AsyncAnthropic,
        registry: ToolRegistry,
        gateway: Gateway,
        ledger: Ledger,
        plans: PlanStore,
        broadcast: Broadcaster,
        planner_model: str,
        max_turns: int,
        agents: AgentRegistry | None = None,
        sandboxes: SandboxManager | None = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.gateway = gateway
        self.ledger = ledger
        self.plans = plans
        self.broadcast = broadcast
        self.planner_model = planner_model
        self.max_turns = max_turns
        self.agents = agents
        self.sandboxes = sandboxes
        self._lock = asyncio.Lock()
        self._running_plan_id: str | None = None

    @property
    def running_plan_id(self) -> str | None:
        return self._running_plan_id

    # ── Entry points ─────────────────────────────────────────────────

    async def run(self, goal: str) -> None:
        """Free chat path. No agent; shared workspace."""
        ctx = RunContext(
            goal=goal,
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            allowed_tools=None,
            agent_name=None,
            sandbox_id=None,
            sandbox_root=None,
            sandbox_capabilities=frozenset(),
            metadata={},
        )
        await self._execute(ctx)

    async def agent_run(self, name: str, task: str) -> None:
        """Run the registered agent `name` against `task`."""
        if self.agents is None or self.sandboxes is None:
            raise RuntimeError("agent subsystem not initialized")
        manifest = self.agents.get(name)
        capabilities = frozenset(manifest.sandbox.capabilities)
        sandbox = await self.sandboxes.get_or_create(
            type=manifest.sandbox.type,
            agent_name=manifest.name,
            profile=manifest.sandbox.profile,
            capabilities=capabilities,
        )
        ctx = RunContext(
            goal=task,
            system_prompt=manifest.system_prompt,
            allowed_tools=list(manifest.tools),
            agent_name=manifest.name,
            sandbox_id=sandbox.description.id,
            sandbox_root=sandbox.description.workspace,
            sandbox_capabilities=capabilities,
            metadata={
                "agent": manifest.name,
                "agent_version": manifest.version,
                "sandbox_id": sandbox.description.id,
                "capabilities": sorted(capabilities),
                "budget": manifest.policy.budget.model_dump(),
            },
        )
        try:
            await self.agents.mark_state(manifest.name, "running")
            await self._execute(ctx)
        finally:
            if self.agents is not None:
                await self.agents.mark_state(manifest.name, "ready")

    # ── Shared loop ──────────────────────────────────────────────────

    async def _execute(self, rc: RunContext) -> None:
        if self._lock.locked():
            raise OrchestratorBusyError("a plan is already running")
        async with self._lock:
            plan = await self.plans.create_plan(
                rc.goal, metadata={**rc.metadata, "agent_name": rc.agent_name}
            )
            self._running_plan_id = plan["id"]
            await self.broadcast("plan.created", plan)
            try:
                await self._drive(plan["id"], rc)
            except anthropic.APIStatusError as e:
                log.exception("anthropic_error", plan_id=plan["id"])
                await self._fail(plan["id"], f"Anthropic API error: {e.message}")
            except Exception as e:
                log.exception("orchestrator_crashed", plan_id=plan["id"])
                await self._fail(plan["id"], f"{type(e).__name__}: {e}")
            finally:
                self._running_plan_id = None

    async def _fail(self, plan_id: str, reason: str) -> None:
        final = await self.plans.finish_plan(plan_id, status="failed")
        await self.broadcast(
            "chat.assistant", {"text": f"Task failed: {reason}", "plan_id": plan_id}
        )
        await self.broadcast("plan.completed", {**final, "error": reason})

    async def _drive(self, plan_id: str, rc: RunContext) -> None:
        tools = self.registry.anthropic_schemas(allow=rc.allowed_tools)
        messages: list[dict[str, Any]] = [{"role": "user", "content": rc.goal}]
        final_text: str = ""

        for turn in range(self.max_turns):
            step = await self.plans.add_step(
                plan_id=plan_id,
                kind="llm",
                description=f"plan turn {turn + 1}",
                risk_class="READ",
            )
            await self.broadcast("plan.step_added", step)

            response = await self.client.messages.create(
                model=self.planner_model,
                max_tokens=16000,
                system=rc.system_prompt,
                tools=tools,
                messages=messages,
                thinking={"type": "adaptive"},
                cache_control={"type": "ephemeral"},
            )

            usage = UsageSnapshot.from_anthropic(response.usage)
            usd = await self.ledger.record_llm(
                plan_id=plan_id,
                step_id=step["id"],
                agent_name=rc.agent_name,
                model=self.planner_model,
                usage=usage,
            )
            step = await self.plans.finish_step(
                step["id"], status="done", cost_usd=usd,
                output={
                    "stop_reason": response.stop_reason,
                    "usage": {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                        "cache_read_input_tokens": usage.cache_read_input_tokens,
                    },
                },
            )
            await self.broadcast("plan.step_updated", step)
            plan = await self.plans.get_plan(plan_id)
            await self.broadcast("cost.updated", {
                "plan_id": plan_id,
                "plan_actual_usd": plan["actual_usd"],
            })

            messages.append({"role": "assistant", "content": response.content})

            text_blocks = [
                b.text for b in response.content if getattr(b, "type", None) == "text"
            ]
            if text_blocks:
                final_text = "\n".join(text_blocks)

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                log.warning(
                    "unexpected_stop_reason",
                    plan_id=plan_id,
                    stop_reason=response.stop_reason,
                )
                break

            tool_uses = [
                b for b in response.content if getattr(b, "type", None) == "tool_use"
            ]
            tool_results_payload: list[dict] = []
            for tu in tool_uses:
                tu_input = dict(tu.input) if tu.input else {}
                step = await self.plans.add_step(
                    plan_id=plan_id,
                    kind="tool",
                    description=f"{tu.name}({_short_args(tu_input)})",
                    risk_class=_tool_risk(self.registry, tu.name),
                    input_data=tu_input,
                )
                await self.broadcast("plan.step_added", step)

                result = await self.gateway.execute(
                    tu.name,
                    tu_input,
                    ctx=ToolContext(
                        plan_id=plan_id,
                        step_id=step["id"],
                        agent_name=rc.agent_name,
                        sandbox_id=rc.sandbox_id,
                        sandbox_root=rc.sandbox_root,
                        sandbox_capabilities=rc.sandbox_capabilities,
                    ),
                )

                step = await self.plans.finish_step(
                    step["id"],
                    status="failed" if result.is_error else "done",
                    output={
                        "content": result.content[:4000],
                        "data": result.data,
                        "risk": result.risk,
                    },
                    error=result.rejection_reason,
                )
                await self.broadcast("plan.step_updated", step)

                tool_results_payload.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result.content,
                    "is_error": result.is_error,
                })

            messages.append({"role": "user", "content": tool_results_payload})

        else:
            log.warning(
                "plan_max_turns_reached", plan_id=plan_id, turns=self.max_turns
            )
            if not final_text:
                final_text = (
                    f"Stopped after {self.max_turns} planning turns without "
                    "finishing. Refine the goal or raise PILK_PLAN_MAX_TURNS."
                )

        final = await self.plans.finish_plan(plan_id, status="completed")
        await self.broadcast(
            "chat.assistant",
            {"text": final_text or "(no response)", "plan_id": plan_id},
        )
        await self.broadcast("plan.completed", final)


def _tool_risk(registry: ToolRegistry, name: str) -> str:
    t = registry.get(name)
    return t.risk.value if t else "READ"


def _short_args(args: dict, limit: int = 80) -> str:
    rendered = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return rendered if len(rendered) <= limit else rendered[: limit - 1] + "…"
