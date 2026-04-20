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

from core.governor import Tier
from core.governor.providers import PlannerProvider, PlannerResponse
from core.ledger import Ledger, UsageSnapshot
from core.logging import get_logger
from core.orchestrator.plans import PlanStore
from core.policy.risk import RiskClass
from core.registry import AgentRegistry
from core.sandbox import SandboxManager
from core.tools import Gateway, ToolRegistry
from core.tools.registry import ToolContext

log = get_logger("pilkd.orchestrator")

Broadcaster = Callable[[str, dict[str, Any]], Awaitable[None]]

DEFAULT_SYSTEM_PROMPT = """You are PILK, a personal execution operating system. The user is
your CEO; you are their COO. Your job is to translate intent into action
— directly when a task is small, or by creating and routing to specialist
agents when it is recurring or specialized.

Your posture:
- You are spoken to as well as typed to. Replies are read aloud by TTS,
  so write for the ear. Short, clear, no bullet spam. No markdown
  headings. One or two sentences per point.
- Refer to the user respectfully. Confirm understanding before launching
  into large or destructive work.

Creating agents (the COO flow):
- When the user says "build me an X agent" or similar, decide adaptively:
  * If the request is clear and scoped (e.g., "a file cleanup agent"),
    propose a name, description, system_prompt, and the smallest adequate
    tool set in one go, then call agent_create. The user sees an
    approval card and confirms.
  * If the request is ambiguous (purpose, data sources, risk level),
    ask 2-4 short follow-ups first. Then propose and call agent_create.
- Name: propose a clean slug (e.g., sales_agent, lead_qualifier). The
  user may rename in the approval card.
- Tools: choose the smallest adequate set. Common picks: fs_read,
  fs_write, shell_exec, net_fetch, llm_ask. Never include
  finance_deposit/withdraw/transfer or trade_execute unless the user
  explicitly asked for financial/trading capability; even then, only pass
  allow_elevated_tools: true after a second clear confirmation.
- system_prompt for the new agent: tight and specific. What it does,
  what it doesn't do, how it reports results.

Routing work to existing agents:
- Before doing a task yourself, check whether a registered agent is the
  right specialist. If so, offer to delegate: "I'll pass this to
  sales_agent — okay?"

Rules of engagement:
- Prefer the cheapest adequate action. Read before you edit. Use
  shell_exec only when a dedicated tool won't do. Use llm_ask for
  bounded sub-tasks.
- Filesystem and shell work is scoped to your workspace. Do not retry
  refused paths with absolute forms — they will refuse too.
- On completion, a one-sentence summary is plenty. No speculative
  follow-up work.

Continuous learning:
- When the user reveals something durable about themselves — a
  preference, a standing rule, a remembered fact, a recurring pattern
  — proactively call memory_remember with the right kind. Examples:
  "I hate markdown headings in voice replies" → preference. "Never
  send emails after 9pm" → standing_instruction. "My assistant's name
  is Maria" → fact. "We usually do sales campaigns on Tuesdays" →
  pattern. Distil, don't transcribe. Don't ask permission for low-
  stakes entries; keep the flow. Do confirm before saving anything
  sensitive (health, financials, relationship details).
- Never save speculative inferences. If you're not sure the user
  meant it as a durable fact about themselves, skip it.
"""


class OrchestratorBusyError(RuntimeError):
    """Raised when a second plan is submitted while one is running."""


class PlanCancelledError(RuntimeError):
    """Raised inside the drive loop when the user has cancelled the plan."""

    def __init__(self, reason: str = "cancelled by user") -> None:
        super().__init__(reason)
        self.reason = reason


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
        governor: Any = None,
        providers: dict[str, PlannerProvider] | None = None,
        sentinel_context_fn: Callable[[], Awaitable[str]] | None = None,
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
        self.governor = governor
        self.providers = providers or {}
        # Optional async hook: returns a short "here's what's broken
        # right now" brief that gets prepended to the system prompt
        # before the first planner turn. ``None`` keeps the legacy
        # behaviour of a perfectly silent orchestrator→sentinel link.
        self.sentinel_context_fn = sentinel_context_fn
        self._lock = asyncio.Lock()
        self._running_plan_id: str | None = None
        self._cancel_event: asyncio.Event | None = None
        self._cancel_reason: str = ""

    @property
    def running_plan_id(self) -> str | None:
        return self._running_plan_id

    async def cancel_plan(self, plan_id: str, *, reason: str = "") -> bool:
        """Request cancellation of `plan_id` if it is the running plan.

        Returns True if a cancel was armed, False if the plan isn't
        currently running. Cancellation is cooperative: the driver checks
        the event between turns and any pending approval tied to the plan
        is force-resolved so the gateway unblocks quickly.
        """
        if self._running_plan_id != plan_id or self._cancel_event is None:
            return False
        self._cancel_reason = reason or "cancelled by user"
        self._cancel_event.set()
        # Unblock any approval this plan is waiting on so the turn
        # finishes quickly instead of sitting on a future that no one
        # will resolve.
        if self.gateway.approvals is not None:
            try:
                await self.gateway.approvals.cancel_plan(
                    plan_id, reason=self._cancel_reason
                )
            except Exception:  # pragma: no cover — best-effort cleanup
                log.warning("approval_cancel_failed", plan_id=plan_id)
        await self.broadcast(
            "plan.cancelling",
            {"plan_id": plan_id, "reason": self._cancel_reason},
        )
        return True

    @staticmethod
    def _supports_thinking(model: str) -> bool:
        """Extended thinking is currently Opus-only on the Messages API."""
        return "opus" in (model or "").lower()

    async def _request_premium_escalation(
        self,
        *,
        plan_id: str,
        rc: RunContext,
        tier_choice: Any,
    ) -> str:
        """Ask the user whether to run this plan on Deep Reasoning.

        Returns the approval decision string: 'approved', 'rejected', or
        'expired'. A rejection (or expiry) keeps the downgraded STANDARD
        tier the Governor already picked.
        """
        assert self.gateway.approvals is not None
        # Read the premium tier so the user sees what they're actually
        # approving (model name, provider).
        premium = self.governor.tiers.get(Tier.PREMIUM) if self.governor else None
        args = {
            "goal": rc.goal[:280],
            "requested_tier": "premium",
            "requested_model": premium.model if premium else "premium",
            "requested_provider": premium.provider if premium else "anthropic",
            "downgraded_to_model": tier_choice.model,
        }
        req = await self.gateway.approvals.request(
            plan_id=plan_id,
            step_id=None,
            agent_name=rc.agent_name,
            tool_name="__premium_escalation",
            args=args,
            risk_class=RiskClass.READ,
            reason=(
                "This task looks like it would benefit from Deep Reasoning "
                "(Opus). Approve to use it for this one task, or reject to "
                "run on Balanced. You can disable 'Ask before Deep Reasoning' "
                "in Settings to skip this prompt."
            ),
            bypass_trust=True,
        )
        try:
            decision = await req.future
        except Exception as e:  # pragma: no cover — unexpected
            log.warning("premium_escalation_future_failed", detail=str(e))
            return "rejected"
        return decision.decision

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
        # Governor daily-cap pre-check — fail fast before we spin a plan
        # if today's spend has already reached the cap.
        if self.governor is not None:
            try:
                await self.governor.check_budget()
            except Exception as e:
                # Produce a visible failed plan so the user sees the reason.
                plan = await self.plans.create_plan(
                    rc.goal,
                    metadata={**rc.metadata, "agent_name": rc.agent_name},
                )
                await self.broadcast("plan.created", plan)
                await self._fail(plan["id"], f"{type(e).__name__}: {e}")
                return
        async with self._lock:
            plan = await self.plans.create_plan(
                rc.goal, metadata={**rc.metadata, "agent_name": rc.agent_name}
            )
            self._running_plan_id = plan["id"]
            self._cancel_event = asyncio.Event()
            self._cancel_reason = ""
            await self.broadcast("plan.created", plan)
            try:
                await self._drive(plan["id"], rc)
            except PlanCancelledError as e:
                log.info("plan_cancelled", plan_id=plan["id"], reason=e.reason)
                await self._cancel(plan["id"], e.reason)
            except anthropic.APIStatusError as e:
                log.exception("anthropic_error", plan_id=plan["id"])
                await self._fail(plan["id"], f"Anthropic API error: {e.message}")
            except Exception as e:
                log.exception("orchestrator_crashed", plan_id=plan["id"])
                await self._fail(plan["id"], f"{type(e).__name__}: {e}")
            finally:
                self._running_plan_id = None
                self._cancel_event = None
                self._cancel_reason = ""

    async def _fail(self, plan_id: str, reason: str) -> None:
        final = await self.plans.finish_plan(plan_id, status="failed")
        await self.broadcast(
            "chat.assistant", {"text": f"Task failed: {reason}", "plan_id": plan_id}
        )
        await self.broadcast("plan.completed", {**final, "error": reason})

    async def _cancel(self, plan_id: str, reason: str) -> None:
        final = await self.plans.finish_plan(plan_id, status="cancelled")
        await self.broadcast(
            "chat.assistant",
            {"text": f"Task cancelled: {reason}", "plan_id": plan_id},
        )
        await self.broadcast("plan.completed", {**final, "cancelled_reason": reason})

    async def _drive(self, plan_id: str, rc: RunContext) -> None:
        tools = self.registry.anthropic_schemas(allow=rc.allowed_tools)
        messages: list[dict[str, Any]] = [{"role": "user", "content": rc.goal}]
        final_text: str = ""

        # Sentinel situational brief: if any unacked incidents exist,
        # prepend them to the system prompt so PILK plans around current
        # trouble instead of learning about it after-the-fact. Empty or
        # None means "all quiet" and we leave the prompt alone.
        effective_system_prompt = rc.system_prompt
        if self.sentinel_context_fn is not None:
            try:
                brief = await self.sentinel_context_fn()
            except Exception as e:
                log.warning("sentinel_context_failed", error=str(e))
                brief = ""
            if brief:
                effective_system_prompt = (
                    f"{brief}\n\n{rc.system_prompt}"
                )

        # The Governor picks the tier for the whole plan based on the
        # original goal. If the classifier wanted PREMIUM but the user
        # has the premium gate on, pick() returns gated=True + a
        # STANDARD choice. We translate that into a real approval so the
        # user can escalate-for-this-task with a single click instead of
        # silently getting Balanced.
        if self.governor is not None:
            tier_choice = self.governor.pick(rc.goal)
            if tier_choice.gated and self.gateway.approvals is not None:
                decision = await self._request_premium_escalation(
                    plan_id=plan_id, rc=rc, tier_choice=tier_choice
                )
                if decision == "approved":
                    # Re-pick with an explicit premium override for this plan.
                    tier_choice = self.governor.pick(rc.goal, override="premium")
                    tier_choice.reason = "gate_approved"
                else:
                    # Keep the downgraded STANDARD choice but flip the flag
                    # so the metadata is accurate.
                    tier_choice.gated = False
                    tier_choice.reason = "gate_declined"
            planner_model = tier_choice.model
            requested_provider = tier_choice.provider
            tier_meta: dict[str, Any] = tier_choice.to_public()
        else:
            planner_model = self.planner_model
            requested_provider = "anthropic"
            tier_meta = {
                "tier": "legacy",
                "provider": "anthropic",
                "model": planner_model,
                "reason": "no_governor",
                "gated": False,
            }

        # Resolve to an actual PlannerProvider; fall back to Anthropic if
        # the requested provider isn't configured.
        provider = self.providers.get(requested_provider)
        effective_provider = requested_provider
        if provider is None:
            provider = self.providers.get("anthropic")
            effective_provider = "anthropic"
            if requested_provider != "anthropic":
                log.warning(
                    "provider_fallback",
                    plan_id=plan_id,
                    requested=requested_provider,
                    effective="anthropic",
                    detail="credentials for requested provider not configured",
                )
        if provider is None:
            # No provider at all — surface a clear failure.
            raise RuntimeError(
                "no planner provider configured (set ANTHROPIC_API_KEY)"
            )
        tier_meta["effective_provider"] = effective_provider

        for turn in range(self.max_turns):
            if self._cancel_event is not None and self._cancel_event.is_set():
                raise PlanCancelledError(self._cancel_reason or "cancelled by user")
            step = await self.plans.add_step(
                plan_id=plan_id,
                kind="llm",
                description=f"plan turn {turn + 1}",
                risk_class="READ",
            )
            await self.broadcast("plan.step_added", step)

            response: PlannerResponse = await provider.plan_turn(
                system=effective_system_prompt,
                messages=messages,
                tools=tools,
                model=planner_model,
                max_tokens=16000,
                use_thinking=self._supports_thinking(planner_model),
                cache_control=True,
            )

            usage = UsageSnapshot.from_anthropic(response.usage)
            usd = await self.ledger.record_llm(
                plan_id=plan_id,
                step_id=step["id"],
                agent_name=rc.agent_name,
                model=planner_model,
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
                    "tier": tier_meta,
                },
            )
            await self.broadcast("plan.step_updated", step)
            plan = await self.plans.get_plan(plan_id)
            await self.broadcast("cost.updated", {
                "plan_id": plan_id,
                "plan_actual_usd": plan["actual_usd"],
            })

            # Serialise the normalized PlannerResponse blocks back into
            # Anthropic-shaped content dicts so they're safe to send to
            # any provider on the next turn.
            assistant_blocks: list[dict[str, Any]] = []
            for b in response.content:
                if getattr(b, "type", None) == "text":
                    assistant_blocks.append({"type": "text", "text": b.text})
                elif getattr(b, "type", None) == "tool_use":
                    assistant_blocks.append(
                        {
                            "type": "tool_use",
                            "id": b.id,
                            "name": b.name,
                            "input": b.input,
                        }
                    )
            messages.append({"role": "assistant", "content": assistant_blocks})

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
                if self._cancel_event is not None and self._cancel_event.is_set():
                    raise PlanCancelledError(
                        self._cancel_reason or "cancelled by user"
                    )
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
