"""Tests for the agent_create meta-tool.

Covers:
  * happy path — writes manifest, registers agent, creates sandbox, broadcasts
  * name validation
  * unknown tool rejection
  * no self-spawning (agent_create in tool list refused)
  * financial/trade tools require allow_elevated_tools
  * trade_execute auto-adds `trading` capability to sandbox
  * policy gate: agent_create always routes to APPROVE and bypasses trust
  * Manifest validator forbids agent_create in an agent's own tool list
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import get_settings
from core.db import ensure_schema
from core.policy import Decision, Gate, GateInput, TrustStore
from core.policy.risk import RiskClass
from core.registry import AgentRegistry
from core.registry.manifest import Manifest
from core.sandbox import SandboxManager
from core.tools import ToolRegistry
from core.tools.builtin import (
    fs_read_tool,
    fs_write_tool,
    make_agent_create_tool,
    net_fetch_tool,
    shell_exec_tool,
    trade_execute_tool,
)
from core.tools.registry import ToolContext


def _wire(agents_dir: Path):
    settings = get_settings()
    ensure_schema(settings.db_path)
    registry = ToolRegistry()
    registry.register(fs_read_tool)
    registry.register(fs_write_tool)
    registry.register(shell_exec_tool)
    registry.register(net_fetch_tool)
    registry.register(trade_execute_tool)
    agent_registry = AgentRegistry(manifests_dir=agents_dir, db_path=settings.db_path)
    sandboxes = SandboxManager(
        sandboxes_dir=settings.sandboxes_dir, db_path=settings.db_path
    )
    events: list[tuple[str, dict]] = []

    async def broadcast(t: str, p: dict) -> None:
        events.append((t, p))

    tool = make_agent_create_tool(
        tool_registry=registry,
        agent_registry=agent_registry,
        sandboxes=sandboxes,
        agents_dir=agents_dir,
        broadcast=broadcast,
    )
    registry.register(tool)
    return tool, agent_registry, sandboxes, events


@pytest.mark.asyncio
async def test_happy_path(tmp_path: Path) -> None:
    tool, reg, sandboxes, events = _wire(tmp_path)
    result = await tool.handler(
        {
            "name": "sales_agent",
            "description": "Qualifies inbound leads and drafts outreach.",
            "system_prompt": (
                "You are the sales_agent. Read leads from fs_read, score them, "
                "and write outreach drafts with fs_write. Never send email."
            ),
            "tools": ["fs_read", "fs_write"],
        },
        ToolContext(),
    )
    assert result.is_error is False, result.content
    assert "sales_agent" in result.content
    manifest_path = tmp_path / "sales_agent" / "manifest.yaml"
    assert manifest_path.exists()

    m = Manifest.load(manifest_path)
    assert m.name == "sales_agent"
    assert m.tools == ["fs_read", "fs_write"]

    # Registered + sandboxed.
    assert "sales_agent" in reg.manifests()
    all_sandboxes = await sandboxes.list_all()
    assert any(s["agent_name"] == "sales_agent" for s in all_sandboxes)

    # Broadcast fired.
    created = [e for e in events if e[0] == "agent.created"]
    assert len(created) == 1
    assert created[0][1]["name"] == "sales_agent"


@pytest.mark.asyncio
async def test_invalid_name_rejected(tmp_path: Path) -> None:
    tool, *_ = _wire(tmp_path)
    result = await tool.handler(
        {
            "name": "Sales-Agent!",
            "description": "x",
            "system_prompt": "a tight system prompt that is long enough",
            "tools": ["fs_read"],
        },
        ToolContext(),
    )
    assert result.is_error is True
    assert "invalid agent name" in result.content


@pytest.mark.asyncio
async def test_unknown_tool_rejected(tmp_path: Path) -> None:
    tool, *_ = _wire(tmp_path)
    result = await tool.handler(
        {
            "name": "ghost_agent",
            "description": "haunts the fs",
            "system_prompt": "a tight system prompt that is long enough",
            "tools": ["fs_read", "does_not_exist"],
        },
        ToolContext(),
    )
    assert result.is_error is True
    assert "unknown tool" in result.content


@pytest.mark.asyncio
async def test_self_spawning_refused(tmp_path: Path) -> None:
    tool, *_ = _wire(tmp_path)
    result = await tool.handler(
        {
            "name": "meta_agent",
            "description": "tries to be COO",
            "system_prompt": "a tight system prompt that is long enough",
            "tools": ["fs_read", "agent_create"],
        },
        ToolContext(),
    )
    assert result.is_error is True
    assert "agent_create" in result.content


@pytest.mark.asyncio
async def test_financial_tools_require_allow_flag(tmp_path: Path) -> None:
    tool, *_ = _wire(tmp_path)
    result = await tool.handler(
        {
            "name": "trader_agent",
            "description": "trades things",
            "system_prompt": "a tight system prompt that is long enough",
            "tools": ["fs_read", "trade_execute"],
        },
        ToolContext(),
    )
    assert result.is_error is True
    assert "trade_execute" in result.content
    assert "allow_elevated_tools" in result.content


@pytest.mark.asyncio
async def test_elevated_flag_auto_adds_trading_capability(tmp_path: Path) -> None:
    tool, reg, *_ = _wire(tmp_path)
    result = await tool.handler(
        {
            "name": "trader_agent",
            "description": "trades things",
            "system_prompt": (
                "You are trader_agent. Only place trades explicitly requested."
            ),
            "tools": ["fs_read", "trade_execute"],
            "allow_elevated_tools": True,
        },
        ToolContext(),
    )
    assert result.is_error is False, result.content
    m = reg.get("trader_agent")
    assert "trading" in m.sandbox.capabilities


def test_policy_routes_agent_create_to_approval() -> None:
    gate = Gate(trust=TrustStore())
    outcome = gate.evaluate(
        GateInput(
            tool_name="agent_create",
            risk=RiskClass.WRITE_LOCAL,
            args={"name": "x"},
        )
    )
    assert outcome.decision is Decision.APPROVE
    assert outcome.bypass_trust is True


def test_policy_trust_cannot_cover_agent_create() -> None:
    gate = Gate(trust=TrustStore())
    gate.trust.add(
        agent_name=None,
        tool_name="agent_create",
        args_matcher={},
        ttl_seconds=60,
    )
    outcome = gate.evaluate(
        GateInput(
            tool_name="agent_create",
            risk=RiskClass.WRITE_LOCAL,
            args={},
        )
    )
    # Even with a trust rule, system sub-policy forces APPROVE.
    assert outcome.decision is Decision.APPROVE


def test_manifest_validator_blocks_agent_create_in_tools() -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate(
            {
                "name": "rogue_agent",
                "description": "tries to be COO",
                "system_prompt": "a tight system prompt that is long enough",
                "tools": ["fs_read", "agent_create"],
                "sandbox": {"type": "process", "profile": "rogue_agent"},
            }
        )
