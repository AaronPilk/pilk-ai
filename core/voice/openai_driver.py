"""OpenAI STT + TTS drivers.

Activated when OPENAI_API_KEY is set. We use the REST API directly via
httpx rather than pulling in the openai SDK — the surface we need is
small (one POST each) and we avoid a second LLM SDK in main deps.

Pricing (as of 2026): whisper-1 is $0.006 per minute, tts-1 is $15 per
1M input characters. We estimate usd on every call so the cost ledger
can record voice spend the same way it records LLM spend.
"""

from __future__ import annotations

import httpx

from core.voice.drivers import SpokenAudio, Transcript

OPENAI_URL = "https://api.openai.com/v1"
WHISPER_MODEL = "whisper-1"
TTS_MODEL = "tts-1"
DEFAULT_VOICE = "alloy"

WHISPER_USD_PER_MINUTE = 0.006
TTS_USD_PER_1M_CHARS = 15.0


class OpenAISTT:
    name = "openai-whisper-1"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def transcribe(
        self, *, audio: bytes, mime_type: str, language: str | None = None
    ) -> Transcript:
        files = {
            "file": ("utterance" + _ext_for(mime_type), audio, mime_type),
            "model": (None, WHISPER_MODEL),
            "response_format": (None, "verbose_json"),
        }
        if language:
            files["language"] = (None, language)
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{OPENAI_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                files=files,
            )
            r.raise_for_status()
            body = r.json()
        duration = float(body.get("duration") or 0.0)
        usd = round(duration / 60.0 * WHISPER_USD_PER_MINUTE, 6)
        return Transcript(
            text=str(body.get("text") or "").strip(),
            language=body.get("language"),
            duration_s=duration,
            usd=usd,
            provider=self.name,
        )


class OpenAITTS:
    name = "openai-tts-1"

    def __init__(self, api_key: str, *, voice: str = DEFAULT_VOICE) -> None:
        self._api_key = api_key
        self._voice = voice

    async def synthesize(self, *, text: str, voice: str | None = None) -> SpokenAudio:
        payload = {
            "model": TTS_MODEL,
            "voice": voice or self._voice,
            "input": text,
            "format": "mp3",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{OPENAI_URL}/audio/speech",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            audio = r.content
        usd = round(len(text) / 1_000_000 * TTS_USD_PER_1M_CHARS, 6)
        return SpokenAudio(
            audio_bytes=audio, mime_type="audio/mpeg", usd=usd, provider=self.name
        )


def _ext_for(mime: str) -> str:
    mapping = {
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
    }
    return mapping.get(mime, ".webm")
