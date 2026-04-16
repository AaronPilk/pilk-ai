"""WebSocket endpoint.

Inbound events from the dashboard:
  chat.user   {id, text} — start a new plan with `text` as the goal.
  ping        {id}        — liveness check; server replies with `pong`.

Outbound events from pilkd (via the hub):
  system.hello, chat.assistant, plan.created, plan.step_added,
  plan.step_updated, plan.completed, cost.updated, system.error.

The WS surface stays stable as features land; new event types only add to
the set. Dashboard routes subscribe by filtering on `type`.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.logging import get_logger

router = APIRouter()
log = get_logger("pilkd.ws")


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    hub = ws.app.state.hub
    orchestrator = ws.app.state.orchestrator

    await ws.accept()
    await hub.add(ws)
    await ws.send_json(
        {
            "type": "system.hello",
            "id": str(uuid.uuid4()),
            "text": "pilkd connected",
            "running_plan_id": (
                orchestrator.running_plan_id if orchestrator else None
            ),
        }
    )
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "system.error", "text": "invalid json"})
                continue

            mtype = msg.get("type")
            if mtype == "chat.user":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                if orchestrator is None:
                    await ws.send_json(
                        {
                            "type": "system.error",
                            "text": (
                                "Orchestrator offline — set ANTHROPIC_API_KEY "
                                "and restart pilkd."
                            ),
                        }
                    )
                    continue
                if orchestrator.running_plan_id is not None:
                    await ws.send_json(
                        {"type": "system.error", "text": "a plan is already running"}
                    )
                    continue
                # Keep a strong reference on app state so GC doesn't cancel mid-run.
                tasks: set[asyncio.Task] = ws.app.state.orchestrator_tasks
                task = asyncio.create_task(orchestrator.run(text))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            elif mtype == "ping":
                await ws.send_json({"type": "pong", "id": msg.get("id")})
            else:
                await ws.send_json(
                    {"type": "system.error", "text": f"unknown type: {mtype}"}
                )
    except WebSocketDisconnect:
        pass
    finally:
        await hub.remove(ws)
