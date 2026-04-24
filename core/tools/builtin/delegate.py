"""delegate_to_agent — the orchestration hand-off tool.

Only the top-level orchestrator gets this tool. When Pilk decides a
registered specialist is a better fit for the current task, he calls
``delegate_to_agent(agent_name, task)``. The handler queues the
delegation on the orchestrator; once Pilk's own plan ends the
orchestrator immediately spins up ``agent_run(name, task)`` so the
specialist takes over with its own system prompt, tool subset, and
sandbox.

Why queue-and-hand-off rather than nest: the orchestrator holds a lock
while a plan runs. Calling ``agent_run`` from inside a tool handler
would deadlock on that lock. Deferring until Pilk's plan finishes keeps
the concurrency model simple and gives the user a clean visual: Pilk
announces the delegation, his plan completes, then the specialist's
plan begins.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from core.logging import get_logger
from core.policy.risk import RiskClass
from core.registry.registry import AgentRegistry
from core.tools.registry import Tool, ToolContext, ToolOutcome

if TYPE_CHECKING:
    from core.orchestrator.orchestrator import Orchestrator

log = get_logger("pilkd.delegate")

Broadcaster = Callable[[str, dict[str, Any]], Awaitable[None]]


def make_delegate_to_agent_tool(
    *,
    agent_registry: AgentRegistry,
    orchestrator_ref: Callable[[], Orchestrator | None],
    broadcast: Broadcaster,
) -> Tool:
    """Build the ``delegate_to_agent`` tool.

    ``orchestrator_ref`` is a zero-arg callable that returns the live
    orchestrator. We take a getter rather than the instance directly
    because the tool is registered before the orchestrator exists
    during startup; binding via closure would capture ``None``.
    """

    async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
        agent_name = str(args.get("agent_name", "")).strip()
        task = str(args.get("task", "")).strip()
        reason = str(args.get("reason", "")).strip()

        if not agent_name:
            return ToolOutcome(
                content="agent_name is required.", is_error=True
            )
        if not task:
            return ToolOutcome(
                content="task is required — write a clear one-sentence goal.",
                is_error=True,
            )

        try:
            manifest = agent_registry.get(agent_name)
        except LookupError:
            known = sorted(agent_registry.manifests().keys())
            return ToolOutcome(
                content=(
                    f"agent {agent_name!r} is not registered. "
                    f"Known agents: {known}"
                ),
                is_error=True,
            )

        orchestrator = orchestrator_ref()
        if orchestrator is None:
            return ToolOutcome(
                content="orchestrator offline; cannot delegate.", is_error=True
            )

        # Queue the delegation. The orchestrator runs it once the
        # current plan releases the lock. Returns False when queueing
        # would exceed MAX_DELEGATION_DEPTH — surface that as a
        # caller-visible refusal rather than a silent drop.
        queued = orchestrator.queue_delegation(agent_name, task)
        if not queued:
            return ToolOutcome(
                content=(
                    f"Refused to delegate to {agent_name}: would exceed "
                    "the maximum delegation chain depth. Complete the "
                    "task directly or return to the parent orchestrator."
                ),
                is_error=True,
            )

        await broadcast(
            "delegation.requested",
            {
                "from": ctx.agent_name or "pilk",
                "to": agent_name,
                "task": task[:400],
                "reason": reason[:400],
                "plan_id": ctx.plan_id,
            },
        )
        log.info(
            "delegation_queued",
            to=agent_name,
            plan_id=ctx.plan_id,
            reason=reason[:120],
        )

        summary = (
            f"Queued delegation to {agent_name}: {manifest.description[:120]}. "
            "It will take over as soon as this plan ends."
        )
        return ToolOutcome(
            content=summary,
            data={
                "agent_name": agent_name,
                "task": task,
                "agent_description": manifest.description,
            },
        )

    return Tool(
        name="delegate_to_agent",
        description=(
            "Hand the current task to a registered specialist agent. Use this "
            "whenever a task fits an agent's purpose better than running it "
            "yourself — it keeps token usage small (the agent loads only its "
            "own tools + system prompt) and lets the specialist's per-agent "
            "memory drive the work. The specialist takes over as soon as your "
            "plan ends. Prefer delegation over direct execution whenever a "
            "fitting agent exists."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": (
                        "The registered agent's slug (e.g. meta_ads_agent, "
                        "sales_ops_agent). Must exactly match a name from "
                        "the catalog in your system prompt."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "A clear, specialist-ready statement of what the "
                        "agent should do. Include any concrete inputs the "
                        "user already supplied (URLs, budgets, targets). "
                        "The agent does not see your conversation — this "
                        "string is the whole task."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One short sentence: why this agent is the right "
                        "fit. Shown in the UI hand-off card."
                    ),
                },
            },
            "required": ["agent_name", "task"],
        },
        risk=RiskClass.READ,
        handler=_handler,
    )
