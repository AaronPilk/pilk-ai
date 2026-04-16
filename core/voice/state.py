"""Voice pipeline state machine.

A single PILK daemon runs one voice interaction at a time. The state
machine below is what keeps that discipline — every transition is made
by the pipeline, never by a UI event directly.

States (from the architecture doc):

  idle          — no mic activity; the default.
  listening     — user holds PTT; audio is being captured client-side.
  transcribing  — audio reached pilkd; STT is running.
  speaking      — TTS is playing back the response.

Transitions:

  idle         → listening       user pressed PTT
  listening    → transcribing    user released PTT; audio uploaded
  listening    → idle            user cancelled before releasing
  transcribing → idle            STT returned empty / errored
  transcribing → speaking        transcript ran; TTS audio ready
  speaking     → idle            TTS finished or was interrupted
  *            → idle            explicit reset (error path)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any


class VoiceState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    SPEAKING = "speaking"


# Allowed transitions. A `reset` edge is allowed from anywhere.
_ALLOWED: dict[VoiceState, frozenset[VoiceState]] = {
    VoiceState.IDLE: frozenset({VoiceState.LISTENING}),
    VoiceState.LISTENING: frozenset({VoiceState.TRANSCRIBING, VoiceState.IDLE}),
    VoiceState.TRANSCRIBING: frozenset({VoiceState.SPEAKING, VoiceState.IDLE}),
    VoiceState.SPEAKING: frozenset({VoiceState.IDLE, VoiceState.LISTENING}),
}


Broadcaster = Callable[[str, dict[str, Any]], Awaitable[None]]


class VoiceStateError(RuntimeError):
    """Raised when a caller requests an illegal state transition."""


class VoiceStateMachine:
    """Thread-safe state machine with a broadcast hook on every change."""

    def __init__(self, *, broadcast: Broadcaster | None = None) -> None:
        self._state = VoiceState.IDLE
        self._broadcast = broadcast
        self._lock = asyncio.Lock()

    @property
    def state(self) -> VoiceState:
        return self._state

    async def transition(
        self, to: VoiceState, *, meta: dict[str, Any] | None = None
    ) -> VoiceState:
        async with self._lock:
            if to == self._state:
                return self._state
            allowed = _ALLOWED.get(self._state, frozenset())
            if to not in allowed:
                raise VoiceStateError(
                    f"illegal transition {self._state.value} → {to.value}"
                )
            self._state = to
        if self._broadcast is not None:
            await self._broadcast(
                "voice.state", {"state": to.value, "meta": meta or {}}
            )
        return to

    async def reset(self, *, meta: dict[str, Any] | None = None) -> VoiceState:
        async with self._lock:
            if self._state is VoiceState.IDLE:
                return self._state
            self._state = VoiceState.IDLE
        if self._broadcast is not None:
            await self._broadcast(
                "voice.state", {"state": VoiceState.IDLE.value, "meta": meta or {}}
            )
        return VoiceState.IDLE
