"""`code_task` — run a coding-oriented request on whichever engine wins routing.

Thin shim over the CodingRouter. The handler picks an engine and calls
`run()`; the outcome surfaces as a ToolOutcome so the orchestrator
attributes it like any other step.

Risk = EXEC_LOCAL because even the draft-only API engine is a remote
call that consumes budget and could generate file-modifying
suggestions. Real filesystem writes still go through `fs_write` +
approvals; this tool never bypasses them.
"""

from __future__ import annotations

from pathlib import Path

from core.coding import CodeTask, CodingRouter
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome


def make_code_task_tool(router: CodingRouter) -> Tool:
    async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
        goal = str(args.get("goal") or "").strip()
        if not goal:
            return ToolOutcome(
                content="code_task requires a 'goal' describing what to do.",
                is_error=True,
            )
        scope = str(args.get("scope") or "function")
        if scope not in ("function", "file", "repo"):
            return ToolOutcome(
                content=f"code_task: unknown scope {scope!r}.",
                is_error=True,
            )
        repo_arg = args.get("repo_path")
        repo_path = Path(str(repo_arg)) if repo_arg else None
        prefer = args.get("prefer_engine")
        task = CodeTask(
            goal=goal,
            scope=scope,  # type: ignore[arg-type]
            repo_path=repo_path,
            prefer_engine=str(prefer) if prefer else None,
        )

        engine = await router.pick(task)
        if engine is None:
            names = router.names()
            return ToolOutcome(
                content=(
                    "No coding engine is available. Configured: "
                    f"{', '.join(names) or '(none)'}."
                ),
                is_error=True,
            )
        result = await engine.run(task)
        if not result.ok:
            return ToolOutcome(content=result.summary, is_error=True)
        body = result.summary
        if result.detail:
            body = f"{result.summary}\n\n{result.detail}"
        return ToolOutcome(
            content=body,
            data={
                "engine": result.engine,
                "usd": result.usd,
                "metadata": result.metadata,
            },
        )

    return Tool(
        name="code_task",
        description=(
            "Draft or run a coding task. Routes to the best available "
            "engine: a local Claude Code bridge for repo-scope work, the "
            "Anthropic Agent SDK as an intermediate fallback, or a bare "
            "Anthropic API call for quick function/file snippets. "
            "File-modifying work still goes through PILK's normal "
            "fs_write + approval flow; this tool does not bypass "
            "approvals."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "What you want the engine to produce.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["function", "file", "repo"],
                    "description": "Routing hint; defaults to 'function'.",
                },
                "repo_path": {
                    "type": "string",
                    "description": "Optional path to the repo for repo-scope tasks.",
                },
                "prefer_engine": {
                    "type": "string",
                    "enum": ["claude-code", "agent-sdk", "api"],
                    "description": "Force a specific engine when healthy.",
                },
            },
            "required": ["goal"],
        },
        risk=RiskClass.EXEC_LOCAL,
        handler=_handler,
    )
