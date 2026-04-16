"""Voice state machine + pipeline tests (stub drivers only)."""

from __future__ import annotations

import pytest

from core.config import get_settings
from core.db import ensure_schema
from core.ledger import Ledger
from core.voice import (
    StubSTT,
    StubTTS,
    VoicePipeline,
    VoiceState,
    VoiceStateError,
    VoiceStateMachine,
)


@pytest.mark.asyncio
async def test_state_transitions_follow_spec() -> None:
    events: list[tuple[str, dict]] = []

    async def broadcast(t: str, p: dict) -> None:
        events.append((t, p))

    sm = VoiceStateMachine(broadcast=broadcast)
    assert sm.state is VoiceState.IDLE

    await sm.transition(VoiceState.LISTENING)
    await sm.transition(VoiceState.TRANSCRIBING)
    await sm.transition(VoiceState.SPEAKING)
    await sm.reset()

    assert all(t == "voice.state" for t, _ in events)
    observed = [p["state"] for _, p in events]
    assert observed == ["listening", "transcribing", "speaking", "idle"]


@pytest.mark.asyncio
async def test_illegal_transition_raises() -> None:
    sm = VoiceStateMachine()
    with pytest.raises(VoiceStateError):
        await sm.transition(VoiceState.SPEAKING)  # idle → speaking is illegal


@pytest.mark.asyncio
async def test_same_state_is_noop() -> None:
    sm = VoiceStateMachine()
    await sm.transition(VoiceState.LISTENING)
    # listening → listening is a no-op; no error.
    await sm.transition(VoiceState.LISTENING)
    assert sm.state is VoiceState.LISTENING


@pytest.mark.asyncio
async def test_stub_pipeline_end_to_end() -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    sm = VoiceStateMachine()
    await sm.transition(VoiceState.LISTENING)  # simulate UI PTT press
    pipeline = VoicePipeline(
        state=sm,
        stt=StubSTT(),
        tts=StubTTS(),
        orchestrator=None,
        ledger=Ledger(settings.db_path),
    )
    result = await pipeline.utter(audio=b"\x00\x01\x02", mime_type="audio/webm")
    assert "stub transcript" in result.transcript
    assert result.response_text.startswith("(orchestrator offline)")
    assert result.audio_b64 and len(result.audio_b64) > 0
    assert result.audio_mime == "audio/wav"
    # Pipeline leaves us in SPEAKING so the client owns the wind-down.
    assert sm.state is VoiceState.SPEAKING


@pytest.mark.asyncio
async def test_empty_transcript_returns_to_idle() -> None:
    class EmptySTT:
        name = "empty"

        async def transcribe(self, *, audio, mime_type, language=None):
            from core.voice.drivers import Transcript
            return Transcript(text="   ", provider="empty")

    settings = get_settings()
    ensure_schema(settings.db_path)
    sm = VoiceStateMachine()
    await sm.transition(VoiceState.LISTENING)
    pipeline = VoicePipeline(
        state=sm,
        stt=EmptySTT(),
        tts=StubTTS(),
        orchestrator=None,
        ledger=Ledger(settings.db_path),
    )
    result = await pipeline.utter(audio=b"x", mime_type="audio/webm")
    assert result.transcript == ""
    assert result.audio_b64 is None
    assert sm.state is VoiceState.IDLE
