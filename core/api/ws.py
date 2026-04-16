"""WebSocket endpoint for the dashboard.

Batch 0: echoes `chat.user` messages back as `chat.reply`. This is the
same socket that later batches extend with plan/step/approval/cost events,
so the dashboard's transport does not change as features land.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.logging import get_logger

router = APIRouter()
log = get_logger("pilkd.ws")


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    await ws.send_json(
        {"type": "system.hello", "id": str(uuid.uuid4()), "text": "pilkd connected"}
    )
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json(
                    {"type": "system.error", "text": "invalid json"}
                )
                continue

            mtype = msg.get("type")
            if mtype == "chat.user":
                text = msg.get("text", "")
                log.info("chat_user", text_len=len(text))
                await ws.send_json(
                    {
                        "type": "chat.reply",
                        "id": str(uuid.uuid4()),
                        "in_reply_to": msg.get("id"),
                        "text": f"echo: {text}",
                    }
                )
            elif mtype == "ping":
                await ws.send_json({"type": "pong", "id": msg.get("id")})
            else:
                await ws.send_json(
                    {"type": "system.error", "text": f"unknown type: {mtype}"}
                )
    except WebSocketDisconnect:
        log.info("ws_disconnected")
