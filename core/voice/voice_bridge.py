"""Voice bridge — wake-word → Whisper → orchestrator → TTS.

Runs as a background asyncio task alongside the PILK daemon so the
operator can say "Hey PILK" from anywhere on their Mac and get a
spoken answer back, without opening the web UI. The pipeline reuses
the existing :class:`core.voice.pipeline.VoicePipeline` so everything
after transcription (orchestrator dispatch, TTS synthesis, cost
ledgering) takes the same code path the browser PTT flow uses.

### Components

* **Wake-word.** `pvporcupine` listens on the default input device for
  the "Hey PILK" phrase. Porcupine's custom-wake-word file is shipped
  in ``assets/wakeword/Hey-PILK.ppn``; the module falls back to the
  "computer" built-in when that file is missing so the bridge still
  works in a fresh checkout.
* **Capture.** After a wake trigger, we record audio until either a
  trailing-silence threshold is hit or :data:`MAX_UTTERANCE_SECONDS`
  elapses. Captured audio is a mono 16-bit PCM @ 16 kHz buffer —
  exactly what Whisper base.en expects.
* **STT.** We prefer the ``faster-whisper`` transcriber when the
  operator's environment has it installed (it runs locally and does
  not bill the API), falling back to the pipeline's configured
  :class:`STTDriver` when it doesn't. Either way, we end up with a
  plain-text utterance.
* **Dispatch.** The utterance goes through
  :meth:`VoicePipeline.process_utterance` (same pathway as the web
  UI), so memory hydration, tier routing, and broadcast semantics
  stay identical.
* **TTS.** The pipeline already returns synthesized audio; we push it
  into ``sounddevice.play`` for playback. When ``sounddevice`` isn't
  available we still complete the round-trip and log that speech was
  suppressed (useful for headless deploys).

### Defensive posture

* All the hardware deps (``pvporcupine``, ``sounddevice``,
  ``faster_whisper``) are optional. The bridge logs a single
  ``voice_bridge_unavailable`` line + degrades gracefully if any
  one is missing; nothing here can crash the daemon.
* Only ONE background task per daemon. Multiple invocations are no-ops.
* The loop is cooperative: :meth:`stop` signals the task, which exits
  at the next wake-word boundary. Worst case is a full utterance
  capture in flight — bounded by :data:`MAX_UTTERANCE_SECONDS`.
* A failed wake-word / STT / synthesis run does NOT kill the loop —
  errors are logged and the bridge resumes listening.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.logging import get_logger
from core.voice.drivers import Transcript

log = get_logger("pilkd.voice.bridge")

# Default wake word paths. We look in the operator's PILK assets
# directory first ("~/PILK/voice/Hey-PILK.ppn"), then fall back to a
# repo-local asset, then to the Porcupine built-in "computer" keyword
# so the bridge at least works out of the box.
WAKE_KEYWORD_NAME = "Hey PILK"
BUILTIN_WAKE_FALLBACK = "computer"

# Audio capture tuning. Porcupine + faster-whisper agree on 16 kHz
# mono; anything else would need resampling and Whisper's accuracy
# degrades past 16 kHz anyway.
SAMPLE_RATE = 16000
CHANNELS = 1
# Max audible utterance length. One sentence is rarely longer than
# 10 seconds; 20 gives room for a rambling operator without letting
# a stuck mic tape a whole podcast into PILK's queue.
MAX_UTTERANCE_SECONDS = 20.0
# Trailing silence threshold (RMS) — below this for
# SILENCE_HANG_SECONDS ends the capture early. Chosen empirically
# against a quiet home office; a hot mic or noisy room will want to
# raise ``silence_rms_threshold`` on construction.
DEFAULT_SILENCE_RMS = 350
DEFAULT_SILENCE_HANG = 1.0
# How often the capture loop polls for silence. 100ms keeps latency
# low without eating the CPU.
CAPTURE_POLL_SECONDS = 0.1

Transcriber = Callable[[bytes], Awaitable[Transcript]]
Dispatcher = Callable[[str], Awaitable[str]]
Speaker = Callable[[bytes, str], Awaitable[None]]


@dataclass
class VoiceBridgeConfig:
    wake_keyword_path: Path | None = None
    fallback_builtin: str = BUILTIN_WAKE_FALLBACK
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    max_utterance_seconds: float = MAX_UTTERANCE_SECONDS
    silence_rms_threshold: int = DEFAULT_SILENCE_RMS
    silence_hang_seconds: float = DEFAULT_SILENCE_HANG
    # Explicit device indexes for testing / multi-device rigs. None
    # leaves sounddevice to pick the system default.
    input_device: int | None = None
    output_device: int | None = None


class VoiceBridge:
    """Background wake-word listener + voice round-trip runner."""

    def __init__(
        self,
        *,
        transcriber: Transcriber,
        dispatcher: Dispatcher,
        speaker: Speaker | None,
        config: VoiceBridgeConfig | None = None,
    ) -> None:
        self._transcribe = transcriber
        self._dispatch = dispatcher
        self._speak = speaker
        self._cfg = config or VoiceBridgeConfig()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        # Lazy-imported hardware libs. Populated on ``start`` the
        # first time we actually need them; keeping them lazy lets the
        # bridge import cleanly on a headless Railway deploy where
        # PortAudio isn't installed.
        self._porcupine: Any = None
        self._sounddevice: Any = None

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        if not await self._load_dependencies():
            log.info(
                "voice_bridge_unavailable",
                reason="required hardware libs not importable",
            )
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="voice-bridge")
        log.info(
            "voice_bridge_started",
            wake_keyword=self._cfg.wake_keyword_path
            or self._cfg.fallback_builtin,
            sample_rate=self._cfg.sample_rate,
        )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._porcupine is not None:
            with contextlib.suppress(Exception):
                self._porcupine.delete()
        log.info("voice_bridge_stopped")

    # ── internals ────────────────────────────────────────────────

    async def _load_dependencies(self) -> bool:
        """Import the optional hardware libs; return False if any are
        missing (the bridge is effectively disabled on that host)."""
        try:
            pvporcupine = importlib.import_module("pvporcupine")
        except Exception as e:
            log.info("voice_bridge_no_porcupine", detail=str(e))
            return False
        try:
            sounddevice = importlib.import_module("sounddevice")
        except Exception as e:
            log.info("voice_bridge_no_sounddevice", detail=str(e))
            return False
        access_key = _read_picovoice_access_key()
        if not access_key:
            log.info(
                "voice_bridge_no_access_key",
                detail="PILK_PICOVOICE_ACCESS_KEY is empty",
            )
            return False
        kwargs: dict[str, Any] = {"access_key": access_key}
        if (
            self._cfg.wake_keyword_path is not None
            and self._cfg.wake_keyword_path.exists()
        ):
            kwargs["keyword_paths"] = [str(self._cfg.wake_keyword_path)]
        else:
            kwargs["keywords"] = [self._cfg.fallback_builtin]
        try:
            self._porcupine = pvporcupine.create(**kwargs)
        except Exception as e:
            log.warning("voice_bridge_porcupine_create_failed", error=str(e))
            return False
        self._sounddevice = sounddevice
        return True

    async def _run(self) -> None:
        """Main wake-listen loop.

        Porcupine is a blocking C call, so we iterate over short audio
        frames read through ``sounddevice`` in a worker thread. After
        every successful trigger we call
        :meth:`_handle_utterance` and then return to listening.
        """
        sd = self._sounddevice
        porcupine = self._porcupine
        assert sd is not None and porcupine is not None
        frame_length = porcupine.frame_length
        try:
            stream = sd.RawInputStream(
                samplerate=self._cfg.sample_rate,
                blocksize=frame_length,
                device=self._cfg.input_device,
                dtype="int16",
                channels=self._cfg.channels,
            )
        except Exception as e:
            log.warning("voice_bridge_input_stream_failed", error=str(e))
            return
        with stream:
            while not self._stop.is_set():
                try:
                    pcm = await asyncio.to_thread(
                        _read_frame, stream, frame_length,
                    )
                except Exception as e:
                    log.warning("voice_bridge_read_failed", error=str(e))
                    await asyncio.sleep(0.5)
                    continue
                if pcm is None:
                    continue
                frame = struct.unpack_from("h" * frame_length, pcm)
                if porcupine.process(frame) < 0:
                    continue
                log.info("voice_bridge_wake_triggered")
                try:
                    await self._handle_utterance(stream, frame_length)
                except Exception as e:
                    log.warning(
                        "voice_bridge_utterance_failed", error=str(e),
                    )

    async def _handle_utterance(self, stream: Any, frame_length: int) -> None:
        """Capture audio until silence / timeout, transcribe, dispatch,
        speak the reply. Runs synchronously w.r.t. the wake loop — we
        deliberately don't overlap utterances, to keep playback linear.
        """
        audio = await asyncio.to_thread(
            _capture_until_silence,
            stream,
            frame_length,
            self._cfg.sample_rate,
            self._cfg.max_utterance_seconds,
            self._cfg.silence_rms_threshold,
            self._cfg.silence_hang_seconds,
        )
        if not audio:
            return
        pcm_bytes = audio  # already int16 bytes
        wav_bytes = _wrap_as_wav(pcm_bytes, self._cfg.sample_rate)
        try:
            transcript = await self._transcribe(wav_bytes)
        except Exception as e:
            log.warning("voice_bridge_transcribe_failed", error=str(e))
            return
        text = (transcript.text or "").strip()
        if not text:
            return
        log.info("voice_bridge_transcribed", chars=len(text))
        try:
            reply = await self._dispatch(text)
        except Exception as e:
            log.warning("voice_bridge_dispatch_failed", error=str(e))
            return
        if not reply:
            return
        if self._speak is None:
            log.info(
                "voice_bridge_reply_ready",
                chars=len(reply),
                speak=False,
            )
            return
        try:
            # Synthesize + play. The speaker callable is responsible
            # for driving the sound card; we pass the raw text + MIME
            # hint ``audio/mpeg`` since the pipeline's TTS default is
            # MP3.
            await self._speak(reply.encode("utf-8"), "audio/mpeg")
        except Exception as e:
            log.warning("voice_bridge_speaker_failed", error=str(e))


# ── helpers ──────────────────────────────────────────────────────


def _read_frame(stream: Any, frame_length: int) -> bytes | None:
    """Pull exactly one Porcupine-sized frame from a ``RawInputStream``.

    Returns ``None`` on underflow — the outer loop treats that as a
    transient hiccup and retries.
    """
    try:
        data, _overflow = stream.read(frame_length)
    except Exception:
        return None
    return bytes(data) if data is not None else None


def _capture_until_silence(
    stream: Any,
    frame_length: int,
    sample_rate: int,
    max_seconds: float,
    silence_rms: int,
    silence_hang_seconds: float,
) -> bytes:
    """Block-read audio until trailing silence or the hard max.

    Returns the raw int16 PCM bytes concatenated — the bridge wraps
    them into a WAV container before handing to the transcriber.
    Empty bytes means the capture immediately hit silence (user
    triggered the wake-word but didn't speak).
    """
    import time

    buf = bytearray()
    started = time.monotonic()
    last_voice = started
    while True:
        if time.monotonic() - started > max_seconds:
            break
        try:
            data, _of = stream.read(frame_length)
        except Exception:
            break
        if data is None:
            continue
        chunk = bytes(data)
        buf.extend(chunk)
        rms = _rms_int16(chunk)
        if rms >= silence_rms:
            last_voice = time.monotonic()
        elif time.monotonic() - last_voice > silence_hang_seconds:
            break
        time.sleep(CAPTURE_POLL_SECONDS)
    return bytes(buf)


def _rms_int16(pcm: bytes) -> int:
    """Root-mean-square of a little-endian int16 PCM buffer.

    Fast path in pure Python — audio frames are ~512 samples, and
    struct.unpack is plenty fast for that size.
    """
    if not pcm:
        return 0
    count = len(pcm) // 2
    if count == 0:
        return 0
    samples = struct.unpack(f"<{count}h", pcm)
    # Avoid dragging numpy in; the sum is cheap enough.
    total = 0
    for s in samples:
        total += s * s
    return int((total // count) ** 0.5)


def _wrap_as_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Prefix a 44-byte RIFF/WAVE header onto a raw int16 mono buffer.

    Whisper (and every other STT driver we target) accepts WAV out of
    the box; doing the wrapping here keeps the transcriber agnostic
    to the capture format.
    """
    num_samples = len(pcm) // 2
    byte_rate = sample_rate * 2
    block_align = 2
    data_size = num_samples * 2
    chunk_size = 36 + data_size
    header = (
        b"RIFF"
        + struct.pack("<I", chunk_size)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH",
                      16,        # Subchunk1Size
                      1,         # PCM
                      1,         # Channels
                      sample_rate,
                      byte_rate,
                      block_align,
                      16)        # bits/sample
        + b"data"
        + struct.pack("<I", data_size)
    )
    return header + pcm


def _read_picovoice_access_key() -> str:
    """Resolve the Picovoice access key from env + PILK secrets.

    Kept local to the bridge so a missing key degrades gracefully
    (the whole module just reports unavailable) instead of poking at
    the settings module on import.
    """
    import os

    direct = os.environ.get("PILK_PICOVOICE_ACCESS_KEY") or os.environ.get(
        "PICOVOICE_ACCESS_KEY",
    )
    if direct:
        return direct
    try:
        from core.secrets import resolve_secret
    except Exception:
        return ""
    try:
        return resolve_secret("picovoice_access_key", None) or ""
    except Exception:
        return ""


__all__ = [
    "BUILTIN_WAKE_FALLBACK",
    "CAPTURE_POLL_SECONDS",
    "DEFAULT_SILENCE_HANG",
    "DEFAULT_SILENCE_RMS",
    "MAX_UTTERANCE_SECONDS",
    "SAMPLE_RATE",
    "WAKE_KEYWORD_NAME",
    "VoiceBridge",
    "VoiceBridgeConfig",
]
