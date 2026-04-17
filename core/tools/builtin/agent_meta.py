"""agent_create — the COO meta-tool.

Only the top-level orchestrator can call this. When the user says
"build me a sales agent", the orchestrator interviews them (or not —
the call site is adaptive), proposes a tool set + name, then invokes
this tool. The user sees an approval card (system sub-policy forces
APPROVE even though the base risk is WRITE_LOCAL) and confirms.

On confirm the tool:

  1. Validates the payload (name pattern, all tools exist, elevated
     tools require explicit opt-in).
  2. Writes `/agents/{name}/manifest.yaml`.
  3. Re-runs the registry's discovery pass so the new agent is in
     memory + in the DB.
  4. Spins up the agent's sandbox so it's ready to run.
  5. Broadcasts `agent.created` so every dashboard tab sees it.

What this tool refuses:

  * A name that doesn't match `^[a-z][a-z0-9_]{1,63}$`.
  * A tool name the registry doesn't know.
  * `agent_create` itself — no self-spawning agents.
  * Financial/irreversible tools unless `allow_elevated_tools: true`
    was passed (which the orchestrator only does after a second,
    explicit user confirmation).
  * A name that already exists — the user deletes the folder manually.

Anything else is a bug — surface it in the approval card's args preview
so the user sees exactly what they're approving.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml

from core.identity import GrantsStore
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.registry.manifest import NAME_PATTERN, Manifest
from core.registry.registry import AgentRegistry
from core.sandbox import SandboxManager
from core.tools.registry import Tool, ToolContext, ToolOutcome, ToolRegistry

log = get_logger("pilkd.agent_create")

# Tools that a freshly-minted agent can never receive without the user
# explicitly setting `allow_elevated_tools: true`. The orchestrator is
# prompted never to set that flag without a second round of confirmation.
ELEVATED_TOOLS: frozenset[str] = frozenset(
    {
        "finance_deposit",
        "finance_withdraw",
        "finance_transfer",
        "trade_execute",
    }
)

# Tools a new agent must never receive, period. Self-spawning is forbidden.
FORBIDDEN_TOOLS: frozenset[str] = frozenset({"agent_create"})

Broadcaster = Callable[[str, dict[str, Any]], Awaitable[None]]


def make_agent_create_tool(
    *,
    tool_registry: ToolRegistry,
    agent_registry: AgentRegistry,
    sandboxes: SandboxManager,
    agents_dir: Path,
    broadcast: Broadcaster,
    grants: GrantsStore | None = None,
) -> Tool:
    async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
        name = str(args.get("name", "")).strip()
        if not NAME_PATTERN.match(name):
            return ToolOutcome(
                content=(
                    f"invalid agent name: {name!r}. Use lowercase letters, "
                    "digits, and underscores; must start with a letter; 2-64 chars."
                ),
                is_error=True,
            )

        description = str(args.get("description", "")).strip()
        if not description:
            return ToolOutcome(
                content="description is required — one short paragraph.",
                is_error=True,
            )

        system_prompt = str(args.get("system_prompt", "")).strip()
        if len(system_prompt) < 20:
            return ToolOutcome(
                content="system_prompt is required and must be substantive.",
                is_error=True,
            )

        raw_tools = args.get("tools") or []
        if not isinstance(raw_tools, list) or not raw_tools:
            return ToolOutcome(
                content="tools must be a non-empty list of tool names.",
                is_error=True,
            )
        tools = [str(t) for t in raw_tools]

        # Validate every tool exists.
        unknown = [t for t in tools if tool_registry.get(t) is None]
        if unknown:
            return ToolOutcome(
                content=f"unknown tool(s): {unknown}", is_error=True
            )

        forbidden = [t for t in tools if t in FORBIDDEN_TOOLS]
        if forbidden:
            return ToolOutcome(
                content=(
                    f"agents may not receive {forbidden}. Only the top-level "
                    "orchestrator can create agents."
                ),
                is_error=True,
            )

        elevated = [t for t in tools if t in ELEVATED_TOOLS]
        if elevated and not bool(args.get("allow_elevated_tools")):
            return ToolOutcome(
                content=(
                    f"{elevated} require explicit user confirmation. "
                    "Ask the user to confirm financial/trading access, then "
                    "retry with allow_elevated_tools: true."
                ),
                is_error=True,
            )

        # Sandbox shape.
        sandbox_type = str(args.get("sandbox_type") or "process")
        sandbox_profile = str(args.get("sandbox_profile") or name)
        sandbox_caps_raw = args.get("sandbox_capabilities") or []
        if not isinstance(sandbox_caps_raw, list):
            return ToolOutcome(
                content="sandbox_capabilities must be a list.", is_error=True
            )
        sandbox_caps = [str(c) for c in sandbox_caps_raw]
        # If they asked for trade_execute, the sandbox needs the trading cap.
        if "trade_execute" in tools and "trading" not in sandbox_caps:
            sandbox_caps.append("trading")

        budget = {
            "per_run_usd": float(args.get("budget_per_run_usd") or 1.00),
            "daily_usd": float(args.get("budget_daily_usd") or 5.00),
        }
        memory_namespace = args.get("memory_namespace")

        manifest_dict: dict[str, Any] = {
            "name": name,
            "version": str(args.get("version") or "0.1.0"),
            "description": description,
            "system_prompt": system_prompt,
            "tools": tools,
            "sandbox": {
                "type": sandbox_type,
                "profile": sandbox_profile,
                "capabilities": sandbox_caps,
            },
            "policy": {"budget": budget},
        }
        if memory_namespace:
            manifest_dict["memory_namespace"] = str(memory_namespace)

        # Validate by round-tripping through the Manifest model.
        try:
            Manifest.model_validate(manifest_dict)
        except Exception as e:
            return ToolOutcome(
                content=f"manifest validation failed: {e}", is_error=True
            )

        agent_dir = agents_dir / name
        if agent_dir.exists():
            return ToolOutcome(
                content=(
                    f"agent '{name}' already exists at {agent_dir}. "
                    "Choose a different name or remove the folder manually."
                ),
                is_error=True,
            )

        try:
            agent_dir.mkdir(parents=True)
            manifest_path = agent_dir / "manifest.yaml"
            manifest_path.write_text(
                yaml.safe_dump(manifest_dict, sort_keys=False),
                encoding="utf-8",
            )
        except OSError as e:
            return ToolOutcome(
                content=f"failed to write manifest: {e}", is_error=True
            )

        # Re-discover — idempotent, picks up the new folder.
        await agent_registry.discover_and_install()

        # New agents start with explicit opt-in semantics: they appear
        # in grants.json with an empty account allow-list until the
        # user grants access from Settings / Agents.
        if grants is not None:
            grants.register_agent(name, accounts=[])

        # Stand up the sandbox so the Agents tab sees it immediately.
        sandbox = await sandboxes.get_or_create(
            type=sandbox_type,
            agent_name=name,
            profile=sandbox_profile,
            capabilities=frozenset(sandbox_caps),
        )

        payload: dict[str, Any] = {
            "name": name,
            "version": manifest_dict["version"],
            "description": description,
            "tools": tools,
            "sandbox": {
                "id": sandbox.description.id,
                "type": sandbox_type,
                "profile": sandbox_profile,
                "capabilities": sandbox_caps,
                "workspace": str(sandbox.description.workspace),
            },
            "budget": budget,
            "elevated": bool(elevated),
            "manifest_path": str(manifest_path),
        }
        await broadcast("agent.created", payload)
        log.info(
            "agent_created",
            name=name,
            tools=tools,
            sandbox=sandbox.description.id,
            elevated=bool(elevated),
        )

        summary = (
            f"Created agent '{name}' with tools {tools}. "
            f"Sandbox {sandbox.description.id} is ready at "
            f"{sandbox.description.workspace}. "
            f"Open the Agents tab and assign it a task."
        )
        if elevated:
            summary += (
                f" NOTE: this agent has elevated capabilities ({elevated}) — "
                "every call still passes through approval."
            )
        return ToolOutcome(content=summary, data=payload)

    return Tool(
        name="agent_create",
        description=(
            "Create and register a new specialist agent. Use this when the user "
            "asks you to build an agent (e.g., 'build me a sales agent'). You are "
            "expected to propose a clean slug name, a tight description, a focused "
            "system_prompt for the new agent, and the smallest adequate tool set. "
            "Financial tools (finance_deposit/withdraw/transfer) and trade_execute "
            "are never included unless the user explicitly asks and you pass "
            "allow_elevated_tools=true after confirming with them. The user sees "
            "an approval card showing exactly what will be created and must confirm."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Slug: lowercase letters, digits, underscores. Must start "
                        "with a letter. 2-64 chars. Example: sales_agent."
                    ),
                },
                "version": {"type": "string", "description": "Semver, default 0.1.0."},
                "description": {
                    "type": "string",
                    "description": "One short paragraph about the agent's purpose.",
                },
                "system_prompt": {
                    "type": "string",
                    "description": (
                        "The agent's operating instructions. Be specific about scope, "
                        "outputs, and what the agent must not do."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Smallest adequate subset of registered tool names. Common "
                        "choices: fs_read, fs_write, shell_exec, net_fetch, llm_ask."
                    ),
                },
                "sandbox_type": {
                    "type": "string",
                    "enum": ["process", "browser", "fs"],
                    "description": "Default process.",
                },
                "sandbox_profile": {
                    "type": "string",
                    "description": "Stable identifier for sandbox persistence. Defaults to name.",
                },
                "sandbox_capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Capability flags. 'trading' unlocks trade_execute; leave empty "
                        "for most agents."
                    ),
                },
                "budget_per_run_usd": {"type": "number"},
                "budget_daily_usd": {"type": "number"},
                "memory_namespace": {"type": "string"},
                "allow_elevated_tools": {
                    "type": "boolean",
                    "description": (
                        "Must be true to include finance/trade tools. Only set after "
                        "an explicit second confirmation from the user."
                    ),
                },
            },
            "required": ["name", "description", "system_prompt", "tools"],
        },
        risk=RiskClass.WRITE_LOCAL,
        handler=_handler,
    )
