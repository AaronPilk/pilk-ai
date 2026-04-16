"""Voice pipeline HTTP surface.

  GET  /voice/status       current state + configured drivers
  POST /voice/listen       UI started capturing (flips state to listening)
  POST /voice/cancel       cancel before release (flips state back to idle)
  POST /voice/utterance    multipart audio upload — runs the pipeline
  POST /voice/done         client-side playback finished (flips state to idle)

Each call is a JSON response; state change broadcasts happen over the WS
hub so any dashboard tab can reflect the indicator without polling.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from core.voice.state import VoiceState, VoiceStateError

router = APIRouter(prefix="/voice")

MAX_UTTERANCE_BYTES = 10 * 1024 * 1024  # 10 MiB — keeps the endpoint cheap
ALLOWED_MIMES = {
    "audio/webm",
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp4",
    "audio/x-m4a",
    "audio/m4a",
}


@router.get("/status")
async def voice_status(request: Request) -> dict[str, Any]:
    state_machine = request.app.state.voice_state
    pipeline = request.app.state.voice_pipeline
    return {
        "state": state_machine.state.value if state_machine else "idle",
        "stt_provider": pipeline.stt.name if pipeline else None,
        "tts_provider": pipeline.tts.name if pipeline else None,
        "enabled": pipeline is not None,
    }


@router.post("/listen")
async def voice_listen(request: Request) -> dict[str, Any]:
    state_machine = request.app.state.voice_state
    if state_machine is None:
        raise HTTPException(status_code=503, detail="voice pipeline offline")
    try:
        await state_machine.transition(VoiceState.LISTENING)
    except VoiceStateError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"state": state_machine.state.value}


@router.post("/cancel")
async def voice_cancel(request: Request) -> dict[str, Any]:
    state_machine = request.app.state.voice_state
    if state_machine is None:
        raise HTTPException(status_code=503, detail="voice pipeline offline")
    await state_machine.reset(meta={"cancelled": True})
    return {"state": state_machine.state.value}


@router.post("/done")
async def voice_done(request: Request) -> dict[str, Any]:
    """Called by the client when TTS playback finishes or is interrupted."""
    state_machine = request.app.state.voice_state
    if state_machine is None:
        raise HTTPException(status_code=503, detail="voice pipeline offline")
    await state_machine.reset(meta={"playback_done": True})
    return {"state": state_machine.state.value}


@router.post("/utterance")
async def voice_utterance(
    request: Request,
    audio: UploadFile = File(...),  # noqa: B008 — FastAPI dependency pattern
    language: str | None = Form(default=None),
) -> dict[str, Any]:
    pipeline = request.app.state.voice_pipeline
    if pipeline is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "voice pipeline offline — install drivers or set "
                "OPENAI_API_KEY / ELEVENLABS_API_KEY to enable cloud drivers"
            ),
        )
    # Browsers send `audio/webm;codecs=opus` — strip the parameters for
    # the allowlist check, but keep the full string for the STT driver
    # (some accept the codec hint).
    raw_mime = audio.content_type or "audio/webm"
    base_mime = raw_mime.split(";", 1)[0].strip().lower()
    if base_mime not in ALLOWED_MIMES:
        raise HTTPException(
            status_code=415, detail=f"unsupported audio mime: {raw_mime}"
        )
    blob = await audio.read()
    if len(blob) == 0:
        raise HTTPException(status_code=400, detail="empty audio upload")
    if len(blob) > MAX_UTTERANCE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"audio too large: {len(blob)} > {MAX_UTTERANCE_BYTES}",
        )
    try:
        result = await pipeline.utter(
            audio=blob, mime_type=base_mime, language=language
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"voice pipeline: {e}") from e
    return {
        "transcript": result.transcript,
        "response_text": result.response_text,
        "audio_b64": result.audio_b64,
        "audio_mime": result.audio_mime,
        "stt_provider": result.stt_provider,
        "tts_provider": result.tts_provider,
        "usd": result.usd,
        "plan_id": result.plan_id,
        "metadata": result.metadata,
    }
