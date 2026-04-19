"""WebSocket broadcast hub + in-process event subscription.

All dashboard tabs share a single WebSocket connection each. The
orchestrator emits events into the hub; the hub fans out to every
connected client. Dead connections are pruned lazily on the next send.
No queueing, no replay — the dashboard hydrates its tabs over REST on
load.

In-process subscribers (Sentinel, future daemons) register a coroutine
callback via :meth:`Hub.subscribe` and receive every broadcast in the
same event loop. Subscriber exceptions are logged and dropped — one
flaky listener cannot break the WebSocket fan-out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket

from core.logging import get_logger

log = get_logger("pilkd.hub")

# A subscriber receives (event_type, payload) once per broadcast.
HubListener = Callable[[str, dict[str, Any]], Awaitable[None]]


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._listeners: list[HubListener] = []
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    def subscribe(self, listener: HubListener) -> None:
        """Register an in-process listener. Cheap + synchronous; the
        listener itself is async and runs in the hub's event loop.

        No de-duplication — call once per process lifetime. Call
        :meth:`unsubscribe` to tear down cleanly at shutdown."""
        self._listeners.append(listener)

    def unsubscribe(self, listener: HubListener) -> None:
        # Removing an already-gone listener is a no-op.
        with contextlib.suppress(ValueError):
            self._listeners.remove(listener)

    async def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        message = json.dumps({"type": event_type, **payload}, default=str)
        async with self._lock:
            targets = list(self._clients)
            listeners = list(self._listeners)
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
        # In-process subscribers run after WebSocket fan-out so a slow
        # listener never delays the UI. Exceptions are swallowed per-
        # listener; a rogue Sentinel rule must not break the hub.
        for listener in listeners:
            try:
                await listener(event_type, payload)
            except Exception as e:
                log.warning(
                    "hub.listener_error",
                    listener=getattr(listener, "__qualname__", repr(listener)),
                    event_type=event_type,
                    error=str(e),
                )
