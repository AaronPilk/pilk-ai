"""Secondary LLM call tool.

Defaults to Haiku 4.5 so the orchestrator (Opus 4.7) can offload cheap
sub-tasks like classification, short summarization, or extraction without
burning Opus tokens. Cost is recorded to the same plan/step that invoked
the call via the ledger bound on app state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome
from core.utils.model_router import route_model

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

    from core.ledger import Ledger


def make_llm_ask_tool(client: AsyncAnthropic, ledger: Ledger, default_model: str) -> Tool:
    async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
        from core.ledger import UsageSnapshot  # local to avoid import cycles at load

        prompt = str(args["prompt"])
        # Model resolution order:
        #   1. explicit `model` arg — caller forced a specific model
        #   2. `task_type` → route_model() — router picks Haiku/Sonnet/Opus
        #   3. `default_model` — Haiku, per the tool's bias toward cheap
        explicit_model = args.get("model")
        task_type = args.get("task_type")
        if explicit_model:
            model = str(explicit_model)
        elif task_type:
            model = route_model(
                str(task_type),
                caller=f"llm_ask:{ctx.agent_name or 'unknown'}",
            )
        else:
            model = default_model
        system = args.get("system")

        kwargs = {
            "model": model,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = str(system)

        response = await client.messages.create(**kwargs)
        text = next(
            (b.text for b in response.content if getattr(b, "type", None) == "text"),
            "",
        )
        await ledger.record_llm(
            plan_id=ctx.plan_id,
            step_id=ctx.step_id,
            agent_name=ctx.agent_name,
            model=model,
            usage=UsageSnapshot.from_anthropic(response.usage),
            # llm_ask always hits the Anthropic SDK directly (no
            # claude_code subscription path), so tag every entry
            # as ``anthropic``. Without this the dashboard's
            # "by provider" rollup misses every llm_ask call.
            tier_provider="anthropic",
        )
        return ToolOutcome(content=text, data={"model": model})

    return Tool(
        name="llm_ask",
        description=(
            "Run a one-shot Claude call for cheap sub-reasoning "
            "(classification, extraction, short summarization). Defaults "
            "to Haiku 4.5. Pass `task_type` (classify / extract / tag / "
            "score / summarize_short / draft / email / copy / reason / "
            "strategy / summarize_long / max) to let the model router "
            "pick the right tier — Haiku for cheap work, Sonnet for "
            "drafting/reasoning, Opus only on `max`. `model` still "
            "overrides when you need a specific model id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "system": {
                    "type": "string",
                    "description": "Optional system prompt.",
                },
                "task_type": {
                    "type": "string",
                    "description": (
                        "Category of work. Routes through "
                        "core.utils.model_router.route_model — cheapest "
                        "model capable of the task wins."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Override for a specific model id (e.g. "
                        "'claude-sonnet-4-6'). Takes precedence over "
                        "`task_type` if both are set."
                    ),
                },
            },
            "required": ["prompt"],
        },
        risk=RiskClass.READ,
        handler=_handler,
    )


# Placeholder export so the package can be imported before an Anthropic client
# is constructed (e.g. from tests). The real tool is built via `make_llm_ask_tool`.
llm_ask_tool = None
