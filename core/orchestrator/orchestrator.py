"""Orchestrator: turns a user goal into a plan and drives the tool loop.

One Claude tool-use loop per goal. We own the loop (not the SDK's tool
runner) so every turn can:
  - create/update a step in the plan store,
  - record LLM usage against the cost ledger (including cache tokens),
  - gate every tool call through the gateway (risk + policy),
  - broadcast live events to connected dashboards.

Model: Opus 4.7 with adaptive thinking (no budget_tokens, no sampling
params — both are rejected on 4.7). Prompt caching is applied at the
top level; tools and system are stable and get the cache hit, messages
grow per turn. Max turns bounded by settings.plan_max_turns.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic

from core.ledger import Ledger, UsageSnapshot
from core.logging import get_logger
from core.orchestrator.plans import PlanStore
from core.tools import Gateway, ToolRegistry
from core.tools.registry import ToolContext

log = get_logger("pilkd.orchestrator")

Broadcaster = Callable[[str, dict[str, Any]], Awaitable[None]]

SYSTEM_PROMPT = """You are PILK, a personal execution operating system.
You receive a user goal, build a plan, and execute it by calling the tools
available to you. You run locally on the user's laptop.

Rules of engagement:
- Prefer the cheapest adequate action. Read files before editing them.
  Use shell.exec only when a dedicated tool won't do. Use llm.ask for
  bounded sub-tasks where a smaller model suffices.
- All filesystem and shell work is scoped to the PILK workspace. If a
  tool refuses a path, don't retry with a different absolute path — it
  will also be refused.
- When a task is complete, finish with a short summary of what you did
  and where the results live. Do not chain extra speculative work.
- Be concise. The user sees your text responses directly in a chat pane.
"""


class OrchestratorBusyError(RuntimeError):
    """Raised when a second plan is submitted while one is running."""


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
    ) -> None:
        self.client = client
        self.registry = registry
        self.gateway = gateway
        self.ledger = ledger
        self.plans = plans
        self.broadcast = broadcast
        self.planner_model = planner_model
        self.max_turns = max_turns
        self._lock = asyncio.Lock()
        self._running_plan_id: str | None = None

    @property
    def running_plan_id(self) -> str | None:
        return self._running_plan_id

    async def run(self, goal: str) -> None:
        """Execute a user goal end-to-end. Serialized — one plan at a time."""
        if self._lock.locked():
            raise OrchestratorBusyError("a plan is already running")
        async with self._lock:
            plan = await self.plans.create_plan(goal)
            self._running_plan_id = plan["id"]
            await self.broadcast("plan.created", plan)
            try:
                await self._drive(plan["id"], goal)
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

    async def _drive(self, plan_id: str, goal: str) -> None:
        tools = self.registry.anthropic_schemas()
        messages: list[dict[str, Any]] = [{"role": "user", "content": goal}]
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
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
                thinking={"type": "adaptive"},
                cache_control={"type": "ephemeral"},
            )

            usage = UsageSnapshot.from_anthropic(response.usage)
            usd = await self.ledger.record_llm(
                plan_id=plan_id,
                step_id=step["id"],
                agent_name=None,
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

            # Always append the full assistant content so tool_use blocks and
            # any compaction metadata survive for the next turn.
            messages.append({"role": "assistant", "content": response.content})

            text_blocks = [
                b.text for b in response.content if getattr(b, "type", None) == "text"
            ]
            if text_blocks:
                final_text = "\n".join(text_blocks)

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                # pause_turn is reserved for server-side tools (not used in batch 1)
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
                    ctx=ToolContext(plan_id=plan_id, step_id=step["id"]),
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
            log.warning("plan_max_turns_reached", plan_id=plan_id, turns=self.max_turns)
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
