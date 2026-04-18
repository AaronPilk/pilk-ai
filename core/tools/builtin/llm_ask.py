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

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

    from core.ledger import Ledger


def make_llm_ask_tool(client: AsyncAnthropic, ledger: Ledger, default_model: str) -> Tool:
    async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
        from core.ledger import UsageSnapshot  # local to avoid import cycles at load

        prompt = str(args["prompt"])
        model = str(args.get("model") or default_model)
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
        )
        return ToolOutcome(content=text, data={"model": model})

    return Tool(
        name="llm_ask",
        description=(
            "Run a one-shot Claude call for cheap sub-reasoning (classification, "
            "extraction, short summarization). Defaults to Haiku 4.5. Use this "
            "instead of Opus for simple, bounded tasks to save tokens."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "system": {
                    "type": "string",
                    "description": "Optional system prompt.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional override, e.g. 'claude-sonnet-4-6'.",
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
