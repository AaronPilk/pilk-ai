"""WebSocket broadcast hub.

All dashboard tabs share a single connection each. The orchestrator emits
events into the hub; the hub fans out to every connected client. Dead
connections are pruned lazily on the next send. No queueing, no replay —
the dashboard hydrates its tabs over REST on load.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket

from core.logging import get_logger

log = get_logger("pilkd.hub")


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        message = json.dumps({"type": event_type, **payload}, default=str)
        async with self._lock:
            targets = list(self._clients)
        stale: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                stale.append(ws)
        if stale:
            async with self._lock:
                for ws in stale:
                    self._clients.discard(ws)
