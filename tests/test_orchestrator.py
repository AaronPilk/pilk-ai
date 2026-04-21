"""End-to-end orchestrator smoke test with a stubbed Anthropic client.

Verifies: plan is created; a tool call is routed through the gateway and
executed; cost is attributed; final assistant text is broadcast; plan
completes with status=completed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from core.config import get_settings
from core.db import ensure_schema
from core.governor.providers import AnthropicPlannerProvider
from core.ledger import Ledger
from core.orchestrator import Orchestrator, PlanStore
from core.policy import Gate
from core.tools import Gateway, ToolRegistry
from core.tools.builtin import fs_read_tool, fs_write_tool

# ── Minimal stand-ins for Anthropic response objects ──────────────────


@dataclass
class _Block:
    type: str
    # text for text blocks
    text: str = ""
    # tool_use fields
    id: str = ""
    name: str = ""
    input: dict | None = None


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _Response:
    content: list[_Block]
    stop_reason: str
    usage: _Usage


class StubMessages:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class StubClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.messages = StubMessages(responses)

    async def close(self):
        pass


# ── Test ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_runs_tool_and_completes() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)

    # Two-turn scripted interaction:
    #   turn 1: Claude asks to write a file via fs_write  → tool_use stop
    #   turn 2: Claude returns a final text              → end_turn
    responses = [
        _Response(
            content=[
                _Block(
                    type="tool_use",
                    id="tu_1",
                    name="fs_write",
                    input={"path": "report.txt", "content": "all green"},
                )
            ],
            stop_reason="tool_use",
            usage=_Usage(input_tokens=100, output_tokens=30, cache_read_input_tokens=0),
        ),
        _Response(
            content=[_Block(type="text", text="Wrote report.txt. Done.")],
            stop_reason="end_turn",
            usage=_Usage(
                input_tokens=50, output_tokens=10, cache_read_input_tokens=80
            ),
        ),
    ]
    client = StubClient(responses)

    ledger = Ledger(settings.db_path)
    plans = PlanStore(settings.db_path)
    registry = ToolRegistry()
    registry.register(fs_read_tool)
    registry.register(fs_write_tool)
    gateway = Gateway(registry, Gate())

    events: list[tuple[str, dict]] = []

    async def broadcast(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    orch = Orchestrator(
        client=client,
        registry=registry,
        gateway=gateway,
        ledger=ledger,
        plans=plans,
        broadcast=broadcast,
        planner_model="claude-opus-4-7",
        max_turns=6,
        providers={"anthropic": AnthropicPlannerProvider(client)},
    )

    await orch.run("write 'all green' to report.txt")

    types = [e[0] for e in events]
    assert "plan.created" in types
    assert "plan.completed" in types
    assert "chat.assistant" in types
    # Two LLM turns + one tool step = at least four step events
    assert types.count("plan.step_added") >= 3

    # Plan persisted and marked completed
    [(_, plan_event)] = [e for e in events if e[0] == "plan.completed"]
    assert plan_event["status"] == "completed"
    assert plan_event["actual_usd"] > 0

    # File was actually written by the tool
    file_path = (settings.workspace_dir / "report.txt").expanduser().resolve()
    assert file_path.exists()
    assert file_path.read_text() == "all green"

    # Cost ledger received two LLM calls
    summary = await ledger.summary()
    assert summary["total_usd"] > 0

    # LLM step output includes the assistant text, so the Tasks UI can
    # render PILK's reply without relying on the (ephemeral)
    # chat.assistant WS event.
    plan_id = plan_event["id"]
    persisted = await plans.get_plan(plan_id)
    llm_steps = [s for s in persisted["steps"] if s["kind"] == "llm"]
    assert llm_steps, "expected at least one persisted llm step"
    # First turn had only a tool_use block — content should be empty
    # string (not missing). Final turn had the text response.
    assert llm_steps[0]["output"]["content"] == ""
    assert llm_steps[-1]["output"]["content"] == "Wrote report.txt. Done."
