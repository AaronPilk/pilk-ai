"""STT/TTS driver protocols + no-op stub implementations.

The voice pipeline is driver-agnostic. Each driver implements a tiny
interface so we can swap providers (cloud, local) without touching the
pipeline. Batch 4 ships two drivers:

  * Stub — always available, no deps, no network. Used by default so the
    pipeline has an end-to-end wire even without an API key. Transcribes
    by returning a canned string; "synthesises" by returning silent PCM.
  * OpenAI — activates when OPENAI_API_KEY is set. Uses whisper-1 for STT
    and tts-1 for TTS. Lives in `core/voice/openai_driver.py`.

More drivers (ElevenLabs, Piper, faster-whisper) slot in the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None = None
    duration_s: float = 0.0
    # USD cost estimate for this call (0.0 when no pricing available).
    usd: float = 0.0
    provider: str = "stub"


@dataclass(frozen=True)
class SpokenAudio:
    audio_bytes: bytes
    mime_type: str = "audio/mpeg"
    # USD cost estimate for this call.
    usd: float = 0.0
    provider: str = "stub"


@runtime_checkable
class STTDriver(Protocol):
    name: str

    async def transcribe(
        self, *, audio: bytes, mime_type: str, language: str | None = None
    ) -> Transcript: ...


@runtime_checkable
class TTSDriver(Protocol):
    name: str

    async def synthesize(self, *, text: str, voice: str | None = None) -> SpokenAudio: ...


# A tiny WAV header for 0-duration silence — enough to register as
# playable audio in the browser without any third-party cost.
_SILENT_WAV = (
    b"RIFF$\x00\x00\x00WAVEfmt "
    b"\x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x40\x1f\x00\x00\x80>\x00\x00"
    b"\x02\x00\x10\x00data\x00\x00\x00\x00"
)


class StubSTT:
    """Echoes a deterministic phrase. Lets the UI wire up without an API key."""

    name = "stub"

    async def transcribe(
        self, *, audio: bytes, mime_type: str, language: str | None = None
    ) -> Transcript:
        # We don't introspect the audio — we just report bytes received.
        return Transcript(
            text=f"(stub transcript — {len(audio)} bytes of audio received)",
            language=language,
            duration_s=0.0,
            provider="stub",
        )


class StubTTS:
    """Emits a near-empty WAV payload so the UI can exercise playback."""

    name = "stub"

    async def synthesize(self, *, text: str, voice: str | None = None) -> SpokenAudio:
        return SpokenAudio(
            audio_bytes=_SILENT_WAV,
            mime_type="audio/wav",
            usd=0.0,
            provider="stub",
        )
