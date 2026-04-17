"""ElevenLabs TTS driver.

Activated when ELEVENLABS_API_KEY is set. Preferred over OpenAI TTS when
both are configured — ElevenLabs voices are materially better and the
provider specialises in speech.

Pricing is per-character and varies by subscription tier; we record the
character count in the cost metadata but don't compute a per-call USD
figure because the user's rate depends on their plan. The ledger will
still attribute characters to the voice step for post-hoc rollup.
"""

from __future__ import annotations

import httpx

from core.voice.drivers import SpokenAudio

ELEVENLABS_URL = "https://api.elevenlabs.io/v1"
# Rachel — a common default voice id. User can override at runtime.
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
DEFAULT_MODEL = "eleven_turbo_v2_5"


class ElevenLabsTTS:
    name = "elevenlabs"

    def __init__(
        self,
        api_key: str,
        *,
        voice_id: str = DEFAULT_VOICE_ID,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model

    async def synthesize(self, *, text: str, voice: str | None = None) -> SpokenAudio:
        voice_id = voice or self._voice_id
        payload = {
            "text": text,
            "model_id": self._model,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{ELEVENLABS_URL}/text-to-speech/{voice_id}",
                headers={
                    "xi-api-key": self._api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json=payload,
            )
            if r.status_code >= 400:
                # Surface ElevenLabs' actual error body so the caller knows
                # whether it's an invalid voice_id, quota, auth, etc.
                body_preview = r.text[:400] if r.text else ""
                raise RuntimeError(
                    f"elevenlabs {r.status_code} (voice_id={voice_id}): {body_preview}"
                )
            audio = r.content
        # We don't compute usd here — ElevenLabs pricing is subscription-
        # tiered. The char count is recorded in the cost ledger metadata.
        return SpokenAudio(
            audio_bytes=audio,
            mime_type="audio/mpeg",
            usd=0.0,
            provider=self.name,
        )
