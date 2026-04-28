"""WebSocket endpoint.

Inbound events from the dashboard:
  chat.user   {id, text, attachments?} — start a new plan.
              `attachments` is an optional [{id: str}, ...] where each
              id was returned by POST /chat/uploads.
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

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.api.auth import decode_auth_context
from core.chat import AttachmentError
from core.config import get_settings
from core.logging import get_logger
from core.orchestrator.orchestrator import ChatAttachment
from core.orchestrator.router import classify_agent

router = APIRouter()
log = get_logger("pilkd.ws")


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    settings = get_settings()
    if settings.cloud:
        token = (ws.query_params.get("token") or "").strip()
        if not token:
            await ws.close(code=4401, reason="missing token")
            return
        jwks_client = None
        if settings.supabase_url:
            jwks_url = (
                settings.supabase_url.rstrip("/")
                + "/auth/v1/.well-known/jwks.json"
            )
            jwks_client = jwt.PyJWKClient(
                jwks_url,
                cache_keys=True,
                lifespan=3600,
            )
        try:
            auth = decode_auth_context(
                token=token,
                jwt_secret=settings.supabase_jwt_secret,
                jwks_client=jwks_client,
            )
        except RuntimeError:
            await ws.close(code=1011, reason="server misconfigured")
            return
        except (jwt.InvalidTokenError, jwt.PyJWTError):
            await ws.close(code=4401, reason="invalid token")
            return
        ws.state.auth = auth

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
                source = str(msg.get("source") or "").strip().lower()
                is_ambient_voice = source in {
                    "ambient_voice",
                    "ambient",
                    "voice_ambient",
                }
                raw_attachments = msg.get("attachments") or []
                if not text and not raw_attachments:
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
                # Resolve attachment IDs → on-disk records before the
                # orchestrator starts, so a missing/corrupt upload fails
                # fast with a user-facing error instead of silently
                # dropping from the prompt.
                attachments: list[ChatAttachment] = []
                attachment_err = None
                store = getattr(ws.app.state, "chat_attachments", None)
                if raw_attachments and store is None:
                    attachment_err = "chat attachment store offline"
                else:
                    try:
                        ids = [
                            str(a.get("id"))
                            for a in raw_attachments
                            if isinstance(a, dict) and a.get("id")
                        ]
                        resolved = store.resolve_many(ids) if ids else []
                        attachments = [
                            ChatAttachment(
                                id=a.id,
                                kind=a.kind,
                                mime=a.mime,
                                filename=a.filename,
                                path=a.path,
                            )
                            for a in resolved
                        ]
                    except AttachmentError as e:
                        attachment_err = str(e)
                if attachment_err:
                    await ws.send_json(
                        {"type": "system.error", "text": attachment_err}
                    )
                    continue
                # Keep a strong reference on app state so GC doesn't cancel mid-run.
                tasks: set[asyncio.Task] = ws.app.state.orchestrator_tasks
                # Pre-route: if the goal unambiguously fits a single
                # registered specialist AND has no attachments (vision
                # work stays with the Pilk planner for tier routing),
                # skip Pilk entirely and dispatch straight to the
                # agent. Keeps tokens small on obvious asks; Pilk still
                # handles everything ambiguous or multi-step.
                agents_registry = ws.app.state.agents
                routed_agent: str | None = None
                if (
                    not attachments
                    and not is_ambient_voice
                    and agents_registry is not None
                ):
                    match = classify_agent(
                        text,
                        agents_registry.manifests().values(),
                    )
                    if match is not None:
                        name, score = match
                        try:
                            agents_registry.get(name)
                        except LookupError:
                            routed_agent = None
                        else:
                            routed_agent = name
                            hub = ws.app.state.hub
                            await hub.broadcast(
                                "chat.routing",
                                {
                                    "goal": text[:400],
                                    "routed_to": name,
                                    "confidence": round(score, 3),
                                    "via": "classifier",
                                },
                            )
                if routed_agent is not None:
                    task = asyncio.create_task(
                        orchestrator.agent_run(routed_agent, text)
                    )
                else:
                    task = asyncio.create_task(
                        orchestrator.run(
                            text,
                            attachments=attachments,
                            preferred_tier="light" if is_ambient_voice else None,
                            suppress_cost_preflight=is_ambient_voice,
                        )
                    )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            elif mtype == "agent.run":
                agent_name = (msg.get("agent") or "").strip()
                task = (msg.get("task") or "").strip()
                if not agent_name or not task:
                    await ws.send_json(
                        {"type": "system.error", "text": "agent and task required"}
                    )
                    continue
                if orchestrator is None:
                    await ws.send_json(
                        {"type": "system.error", "text": "orchestrator offline"}
                    )
                    continue
                if orchestrator.running_plan_id is not None:
                    await ws.send_json(
                        {"type": "system.error", "text": "a plan is already running"}
                    )
                    continue
                registry = ws.app.state.agents
                if registry is None:
                    await ws.send_json(
                        {"type": "system.error", "text": "agent registry offline"}
                    )
                    continue
                try:
                    registry.get(agent_name)
                except LookupError as e:
                    await ws.send_json(
                        {"type": "system.error", "text": str(e)}
                    )
                    continue
                tasks: set[asyncio.Task] = ws.app.state.orchestrator_tasks
                task_handle = asyncio.create_task(
                    orchestrator.agent_run(agent_name, task)
                )
                tasks.add(task_handle)
                task_handle.add_done_callback(tasks.discard)
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
