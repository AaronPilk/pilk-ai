"""End-to-end voice pipeline.

Takes one audio blob, transcribes, routes the transcript through the
orchestrator, synthesises the response, returns it. The orchestrator is
optional — when it's offline (no API key) we echo the transcript back as
the "assistant" text so the UI still has something to play.

State discipline:

  idle → transcribing → (speaking | idle)

The LISTENING state is set and cleared by the HTTP layer around this
call: LISTENING is client-side (user holding PTT). By the time the audio
hits pilkd, we're already past LISTENING and the pipeline owns the rest.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from core.ledger import Ledger
from core.logging import get_logger
from core.orchestrator import Orchestrator
from core.voice.drivers import STTDriver, TTSDriver
from core.voice.state import VoiceState, VoiceStateMachine

log = get_logger("pilkd.voice")


@dataclass
class VoiceResult:
    transcript: str
    response_text: str
    audio_b64: str | None
    audio_mime: str
    stt_provider: str
    tts_provider: str
    usd: float
    plan_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class VoicePipeline:
    """Coordinates STT → orchestrator → TTS for a single utterance."""

    def __init__(
        self,
        *,
        state: VoiceStateMachine,
        stt: STTDriver,
        tts: TTSDriver,
        orchestrator: Orchestrator | None,
        ledger: Ledger,
    ) -> None:
        self.state = state
        self.stt = stt
        self.tts = tts
        self.orchestrator = orchestrator
        self.ledger = ledger
        self._lock = asyncio.Lock()

    async def utter(
        self,
        *,
        audio: bytes,
        mime_type: str,
        language: str | None = None,
    ) -> VoiceResult:
        async with self._lock:
            if self.state.state is not VoiceState.LISTENING:
                # Accept audio even if we missed the LISTENING event (e.g.
                # the UI skipped that hint). Jump straight to transcribing.
                await self.state.reset()
            await self.state.transition(VoiceState.TRANSCRIBING)
            try:
                transcript = await self.stt.transcribe(
                    audio=audio, mime_type=mime_type, language=language
                )
            except Exception as e:
                log.exception("voice_stt_failed")
                await self.state.reset(meta={"error": str(e)})
                raise

            text = transcript.text.strip()
            await self._record_voice_cost(
                kind="stt",
                provider=transcript.provider,
                usd=transcript.usd,
                metadata={
                    "duration_s": transcript.duration_s,
                    "language": transcript.language,
                    "chars": len(text),
                },
            )

            if not text:
                await self.state.reset(meta={"empty": True})
                return VoiceResult(
                    transcript="",
                    response_text="(no speech detected)",
                    audio_b64=None,
                    audio_mime="",
                    stt_provider=transcript.provider,
                    tts_provider="",
                    usd=transcript.usd,
                )

            # Route through the orchestrator if we can; otherwise just echo.
            plan_id: str | None = None
            response_text: str
            if self.orchestrator is not None:
                response_text, plan_id = await self._route(text)
            else:
                response_text = f"(orchestrator offline) you said: {text}"

            await self.state.transition(
                VoiceState.SPEAKING,
                meta={"transcript": text, "plan_id": plan_id},
            )
            try:
                spoken = await self.tts.synthesize(text=response_text)
            except Exception as e:
                log.exception("voice_tts_failed")
                await self.state.reset(meta={"error": str(e)})
                raise
            await self._record_voice_cost(
                kind="tts",
                provider=spoken.provider,
                usd=spoken.usd,
                metadata={"chars": len(response_text)},
            )

            # Stay in SPEAKING so the client can drive the transition to
            # IDLE when playback ends; if the client never pings, the next
            # PTT press will reset us to LISTENING.
            import base64

            audio_b64 = base64.b64encode(spoken.audio_bytes).decode("ascii")
            return VoiceResult(
                transcript=text,
                response_text=response_text,
                audio_b64=audio_b64,
                audio_mime=spoken.mime_type,
                stt_provider=transcript.provider,
                tts_provider=spoken.provider,
                usd=round(transcript.usd + spoken.usd, 6),
                plan_id=plan_id,
                metadata={
                    "stt_duration_s": transcript.duration_s,
                    "tts_bytes": len(spoken.audio_bytes),
                },
            )

    async def _route(self, text: str) -> tuple[str, str | None]:
        """Run one orchestrator turn and return (response_text, plan_id).

        Voice routes through the *same* orchestrator as typed chat — that
        is explicitly required by the architecture doc (no duplicate
        brains). We capture the assistant text + plan id by sitting on
        the broadcast hub temporarily.
        """
        assert self.orchestrator is not None
        collected: dict[str, Any] = {"text": "", "plan_id": None}
        event = asyncio.Event()
        original_broadcast = self.orchestrator.broadcast

        async def tap(event_type: str, payload: dict) -> None:
            await original_broadcast(event_type, payload)
            if event_type == "plan.created":
                collected["plan_id"] = payload.get("id")
            elif event_type == "chat.assistant":
                collected["text"] = payload.get("text", "")
            elif event_type == "plan.completed":
                event.set()

        self.orchestrator.broadcast = tap
        try:
            await self.orchestrator.run(text)
            # `run` awaits until completion, but plan.completed may arrive
            # microseconds after — give it a short settle.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=0.5)
        finally:
            self.orchestrator.broadcast = original_broadcast
        return collected["text"] or "(no response)", collected["plan_id"]

    async def _record_voice_cost(
        self,
        *,
        kind: str,
        provider: str,
        usd: float,
        metadata: dict[str, Any],
    ) -> None:
        if usd <= 0 and provider == "stub":
            return
        import json
        from datetime import UTC, datetime

        from core.db import connect

        async with connect(self.ledger.db_path) as conn:
            await conn.execute(
                "INSERT INTO cost_entries(plan_id, step_id, agent_name, "
                "kind, model, input_tokens, output_tokens, usd, occurred_at, "
                "metadata_json) VALUES (NULL, NULL, NULL, ?, ?, NULL, NULL, "
                "?, ?, ?)",
                (
                    f"voice.{kind}",
                    provider,
                    round(usd, 6),
                    datetime.now(UTC).isoformat(),
                    json.dumps(metadata),
                ),
            )
            await conn.commit()
