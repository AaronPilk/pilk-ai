"""``analyze_video_url`` — watch and analyze a public video at a URL.

When the operator drops a TikTok / Instagram / YouTube / Twitter link
into chat or Telegram and says "what is this", "summarize this",
"could we use this", PILK runs this tool. The flow:

1. Download the video with ``yt-dlp`` to a tmp dir (capped at 300 MB).
2. Probe duration via ``ffprobe``, then extract frames at 1 fps so
   nothing visual gets missed in short-form content. Frame count is
   capped at 60 so a longer clip drops fps automatically rather than
   blowing the per-call image budget. Operator can override with
   ``n_frames`` when they want even denser sampling on a short clip.
3. Strip the audio track and transcribe via OpenAI Whisper.
4. Build a multimodal Claude call (Sonnet 4.6) with the keyframes as
   image blocks and the transcript as a text block, plus the operator's
   question (or a default 'summarize + flag actionable ideas' prompt).
5. Return the analysis text. The tmp dir is cleaned up regardless of
   outcome.

Risk class is NET_READ — public download, no side effects beyond a
brief tmp-dir write that's cleaned on exit. Auto-allowed.

Designed so each step is mockable for tests via constructor seams,
keeping the test suite fast and offline.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

log = get_logger("pilkd.video_analyze")

# Frame budget. The operator's case is short-form (TikTok / Reels /
# YouTube Shorts — typically under 60s, sometimes a full 2-3 min).
# We sample at 1 fps so every second gets a frame; the cap below
# is intentionally generous because cost flows through the
# operator's Anthropic subscription, not per-token API spend. 120
# frames covers a 2-minute clip in full at 1 fps — beyond that fps
# drops to fit. If a future Claude release lowers the per-request
# image cap, drop ``MAX_FRAMES`` to match.
DEFAULT_FRAMES = 0              # 0 means "auto: 1 fps, cap MAX_FRAMES"
MAX_FRAMES = 120
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
MAX_VIDEO_SECONDS = 600         # 10 min hard cap; longer needs chunking
PROBE_TIMEOUT_S = 30.0
DEFAULT_FRAMES_PER_SECOND = 1.0
MAX_TRANSCRIPT_CHARS = 8000     # truncated to keep token cost bounded
DOWNLOAD_TIMEOUT_S = 300.0      # 5 min — Instagram + TikTok are seconds
FFMPEG_TIMEOUT_S = 120.0
WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"
WHISPER_TIMEOUT_S = 120.0
DEFAULT_ANALYSIS_MODEL = "claude-sonnet-4-6"
ANALYSIS_MAX_TOKENS = 1500
HTTP_URL_PREFIXES = ("http://", "https://")

DEFAULT_QUESTION = (
    "Summarize this video clearly. Identify any concrete techniques, "
    "code patterns, product ideas, growth tactics, or workflows the "
    "operator might want to use or build into PILK. Be specific about "
    "what would be worth implementing and what's just talk."
)

ANALYSIS_SYSTEM = (
    "You are PILK's video analyst. The operator sent a short-form "
    "video (TikTok / Instagram / YouTube / similar) and wants your "
    "read. You see (a) several keyframes from the video as images "
    "and (b) the audio transcript. Synthesize both — the visual "
    "matters (someone showing code on screen, a UI demo, before/after "
    "comparison) and the audio matters (claims, instructions, hype "
    "level). Reply in plain English so the operator (non-coder) can "
    "follow. If the video is mostly hype with no substance, say so. "
    "If there's something concrete worth implementing, name it and "
    "say roughly how PILK could try it."
)

VideoDownloader = Callable[[str, Path], Awaitable[Path]]
DurationProber = Callable[[Path], Awaitable[float]]
# ``FrameExtractor(video_path, frames_dir, n_frames, duration_s)``
# — duration_s is 0.0 when the prober couldn't determine it.
FrameExtractor = Callable[
    [Path, Path, int, float], Awaitable[list[Path]]
]
AudioExtractor = Callable[[Path, Path], Awaitable[Path | None]]
Transcriber = Callable[[Path], Awaitable[str]]
VideoAnalyzer = Callable[
    [list[Path], str, str], Awaitable[str]
]


def _is_http_url(s: str) -> bool:
    return any(s.startswith(p) for p in HTTP_URL_PREFIXES)


async def _run(cmd: list[str], *, timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except TimeoutError as e:
        proc.kill()
        raise RuntimeError(f"{cmd[0]} timed out") from e
    return (
        proc.returncode or 0,
        (stdout or b"").decode("utf-8", errors="replace"),
        (stderr or b"").decode("utf-8", errors="replace"),
    )


async def _default_download(url: str, dest_dir: Path) -> Path:
    """Run ``yt-dlp`` to fetch the video into ``dest_dir``. Caps file
    size + duration so a runaway URL can't fill the disk."""
    out_template = str(dest_dir / "video.%(ext)s")
    rc, stdout, stderr = await _run(
        [
            "yt-dlp",
            "--no-playlist",
            "--max-filesize", str(MAX_DOWNLOAD_BYTES),
            "--match-filter", f"duration < {MAX_VIDEO_SECONDS}",
            "--format", "best[ext=mp4]/best",
            "-o", out_template,
            url,
        ],
        timeout=DOWNLOAD_TIMEOUT_S,
    )
    if rc != 0:
        raise RuntimeError(
            f"yt-dlp failed (rc={rc}): "
            f"{(stderr or stdout)[:300] or 'no output'}"
        )
    matches = sorted(dest_dir.glob("video.*"))
    if not matches:
        raise RuntimeError(
            "yt-dlp succeeded but no video file landed — the URL may "
            "be a slideshow or a live stream we can't capture."
        )
    return matches[0]


async def _default_probe_duration(video_path: Path) -> float:
    """Read the video's duration in seconds via ffprobe. Returns
    0.0 if the probe fails — the caller falls back to a fixed
    sampling rate, which still yields a usable analysis."""
    rc, stdout, _stderr = await _run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(video_path),
        ],
        timeout=PROBE_TIMEOUT_S,
    )
    if rc != 0:
        return 0.0
    try:
        return max(0.0, float(stdout.strip() or 0.0))
    except ValueError:
        return 0.0


def _plan_frame_extraction(
    duration_s: float, requested_frames: int,
) -> tuple[float, int]:
    """Decide the (fps, frame_count) tuple to feed ffmpeg.

    Strategy:
    - If the operator pinned ``requested_frames`` (>0), honour it and
      pick fps to spread that many frames evenly across the duration.
    - Otherwise default to 1 fps so every second of short-form
      content gets a frame, capped at MAX_FRAMES. Past the cap, fps
      drops to MAX_FRAMES / duration so we still cover the full clip
      with the budget we have.

    A duration of 0.0 means ffprobe couldn't tell us — fall back to
    1 fps capped at MAX_FRAMES; ffmpeg's ``-frames:v`` will stop
    early if the source is shorter than the implied window.
    """
    if requested_frames and requested_frames > 0:
        target = max(1, min(int(requested_frames), MAX_FRAMES))
        fps = (
            target / duration_s
            if duration_s > 0
            else DEFAULT_FRAMES_PER_SECOND
        )
        return (fps, target)

    if duration_s <= 0:
        return (DEFAULT_FRAMES_PER_SECOND, MAX_FRAMES)

    natural = int(duration_s * DEFAULT_FRAMES_PER_SECOND) + 1
    if natural <= MAX_FRAMES:
        return (DEFAULT_FRAMES_PER_SECOND, natural)
    fps = MAX_FRAMES / duration_s
    return (fps, MAX_FRAMES)


async def _default_extract_frames(
    video_path: Path, frames_dir: Path, n_frames: int,
    *, duration_s: float = 0.0,
) -> list[Path]:
    """Sample frames densely enough to give Claude vision the whole
    video. Default behaviour is 1 fps (i.e. every second gets a
    frame), capped at MAX_FRAMES; for clips longer than that, fps
    drops to fit the budget. ``n_frames`` is treated as a target
    count when nonzero — ffmpeg picks the implied fps to spread that
    many evenly across the duration. Frames are resized to max
    1024 px wide so each stays well under the per-image token cost.
    """
    frames_dir.mkdir(parents=True, exist_ok=True)
    out_pattern = str(frames_dir / "frame_%03d.jpg")
    fps, target_count = _plan_frame_extraction(duration_s, n_frames)
    rc, _stdout, stderr = await _run(
        [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-vf", f"fps={fps:.6f},scale='min(1024,iw)':-2",
            "-frames:v", str(target_count),
            "-q:v", "5",
            out_pattern,
        ],
        timeout=FFMPEG_TIMEOUT_S,
    )
    if rc != 0:
        raise RuntimeError(
            f"ffmpeg frame extraction failed: {stderr[:300]}"
        )
    return sorted(frames_dir.glob("frame_*.jpg"))


async def _default_extract_audio(
    video_path: Path, dest_dir: Path,
) -> Path | None:
    """Strip the audio track to an mp3. Returns ``None`` if the
    source has no audio (some Instagram clips are silent)."""
    out_path = dest_dir / "audio.mp3"
    rc, _stdout, stderr = await _run(
        [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "64k",
            str(out_path),
        ],
        timeout=FFMPEG_TIMEOUT_S,
    )
    if rc != 0:
        # No audio stream is a normal case (silent clip), not an error.
        if "no audio streams" in stderr.lower():
            return None
        raise RuntimeError(
            f"ffmpeg audio extraction failed: {stderr[:300]}"
        )
    if not out_path.exists() or out_path.stat().st_size < 1024:
        return None
    return out_path


def _make_default_transcriber(api_key: str | None) -> Transcriber:
    async def _transcribe(audio_path: Path) -> str:
        if not api_key:
            return ""
        async with httpx.AsyncClient(
            timeout=WHISPER_TIMEOUT_S,
        ) as client:
            with audio_path.open("rb") as fh:
                files = {
                    "file": (audio_path.name, fh.read(), "audio/mpeg"),
                    "model": (None, WHISPER_MODEL),
                    "response_format": (None, "text"),
                }
            resp = await client.post(
                WHISPER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Whisper {resp.status_code}: {resp.text[:300]}"
                )
            return resp.text.strip()

    return _transcribe


def _make_default_analyzer(
    client: AsyncAnthropic, model: str,
) -> VideoAnalyzer:
    async def _analyze(
        frame_paths: list[Path],
        transcript: str,
        question: str,
    ) -> str:
        content_blocks: list[dict[str, Any]] = []
        for fp in frame_paths:
            data = base64.standard_b64encode(
                fp.read_bytes()
            ).decode("ascii")
            content_blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": data,
                    },
                }
            )
        text_parts = [f"Operator's question:\n{question}"]
        if transcript:
            truncated = transcript[:MAX_TRANSCRIPT_CHARS]
            tail = (
                "\n[truncated]"
                if len(transcript) > MAX_TRANSCRIPT_CHARS
                else ""
            )
            text_parts.append(
                f"\n\nAudio transcript:\n{truncated}{tail}"
            )
        else:
            text_parts.append(
                "\n\n(No usable audio — analyse the visuals alone.)"
            )
        content_blocks.append(
            {"type": "text", "text": "\n".join(text_parts)}
        )
        resp = await client.messages.create(
            model=model,
            max_tokens=ANALYSIS_MAX_TOKENS,
            system=ANALYSIS_SYSTEM,
            messages=[{"role": "user", "content": content_blocks}],
        )
        text = ""
        for block in resp.content or []:
            if getattr(block, "type", None) == "text":
                text += getattr(block, "text", "")
        return text.strip()

    return _analyze


def make_analyze_video_url_tool(
    anthropic_client: AsyncAnthropic,
    *,
    openai_api_key: str | None = None,
    analysis_model: str = DEFAULT_ANALYSIS_MODEL,
    downloader: VideoDownloader | None = None,
    duration_prober: DurationProber | None = None,
    frame_extractor: FrameExtractor | None = None,
    audio_extractor: AudioExtractor | None = None,
    transcriber: Transcriber | None = None,
    analyzer: VideoAnalyzer | None = None,
) -> Tool:
    """Factory. ``anthropic_client`` is used for the multimodal
    analysis call. The OpenAI key is for Whisper transcription —
    omit if you want visuals-only analysis. Tests inject the seam
    callables to keep the run offline."""

    download = downloader or _default_download
    probe_duration = duration_prober or _default_probe_duration
    frames = frame_extractor or _default_extract_frames
    audio = audio_extractor or _default_extract_audio
    api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
    transcribe = transcriber or _make_default_transcriber(api_key)
    analyze = analyzer or _make_default_analyzer(
        anthropic_client, analysis_model,
    )

    async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
        url = str(args.get("url") or "").strip()
        if not url or not _is_http_url(url):
            return ToolOutcome(
                content=(
                    "analyze_video_url needs a 'url' starting with "
                    "http:// or https:// pointing at a public video "
                    "(Instagram reel, TikTok, YouTube short, etc.)."
                ),
                is_error=True,
            )
        question = str(args.get("question") or DEFAULT_QUESTION).strip()
        # Frame count is advisory — 0 (the default) means
        # "auto: 1 fps capped at MAX_FRAMES based on actual duration".
        # An explicit override is bounded to [1, MAX_FRAMES] so a
        # typo can't blow the per-call image budget.
        raw_frames = args.get("n_frames")
        if raw_frames is None:
            n_frames = DEFAULT_FRAMES
        else:
            try:
                n_frames = int(raw_frames)
            except (TypeError, ValueError):
                n_frames = DEFAULT_FRAMES
            n_frames = (
                max(1, min(n_frames, MAX_FRAMES))
                if n_frames > 0
                else DEFAULT_FRAMES
            )

        tmpdir = Path(tempfile.mkdtemp(prefix="pilk-video-"))
        try:
            try:
                video_path = await download(url, tmpdir)
            except Exception as e:
                log.exception("video_download_failed", url=url)
                return ToolOutcome(
                    content=(
                        f"Couldn't download the video. "
                        f"{type(e).__name__}: {e}. The link may be "
                        "private, geo-blocked, or longer than the "
                        "10-minute cap."
                    ),
                    is_error=True,
                )
            # Probe duration first so frame extraction can adapt: a
            # 30 s clip needs ~30 frames at 1 fps for full coverage,
            # not the legacy fixed 8. Probe failure is non-fatal —
            # the extractor falls back to 1 fps with the cap.
            try:
                duration_s = await probe_duration(video_path)
            except Exception:
                log.exception("video_duration_probe_failed", url=url)
                duration_s = 0.0
            frames_dir = tmpdir / "frames"
            try:
                frame_paths = await frames(
                    video_path, frames_dir, n_frames, duration_s,
                )
            except Exception as e:
                log.exception("video_frame_extract_failed", url=url)
                return ToolOutcome(
                    content=(
                        f"Couldn't extract frames from the video. "
                        f"{type(e).__name__}: {e}."
                    ),
                    is_error=True,
                )
            if not frame_paths:
                return ToolOutcome(
                    content=(
                        "ffmpeg returned zero frames — the file may "
                        "be corrupted or have an unsupported codec."
                    ),
                    is_error=True,
                )

            transcript = ""
            try:
                audio_path = await audio(video_path, tmpdir)
                if audio_path is not None and api_key:
                    transcript = await transcribe(audio_path)
            except Exception:
                # Audio is best-effort; visuals carry the analysis.
                log.exception("video_audio_pipeline_failed", url=url)
                transcript = ""

            try:
                analysis = await analyze(
                    frame_paths, transcript, question,
                )
            except Exception as e:
                log.exception("video_analyze_call_failed", url=url)
                return ToolOutcome(
                    content=(
                        f"Frames were extracted but the analysis "
                        f"call failed: {type(e).__name__}: {e}."
                    ),
                    is_error=True,
                )

            log.info(
                "video_analyze_completed",
                url=url,
                frames=len(frame_paths),
                transcript_chars=len(transcript),
                model=analysis_model,
            )
            return ToolOutcome(
                content=analysis,
                data={
                    "url": url,
                    "frames_used": len(frame_paths),
                    "transcript_chars": len(transcript),
                    "model": analysis_model,
                    "had_audio": bool(transcript),
                    "duration_s": duration_s,
                },
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return Tool(
        name="analyze_video_url",
        description=(
            "Watch and analyze a PUBLIC video at a URL (TikTok, "
            "YouTube short, Twitter clip, public Instagram reel). "
            "For private / login-walled content (most Instagram "
            "Reels, gated TikToks, paid videos), the operator "
            "downloads the file and uploads it to Telegram or chat "
            "— that path goes through ``analyze_video_file`` instead. "
            "Downloads the video, samples one frame per second across "
            "the entire clip (so nothing visual is missed), "
            "transcribes the audio with Whisper, and asks Claude "
            "vision for an analysis. Default behaviour gives full "
            "coverage on short-form (≤2 min) without the operator "
            "needing to specify frame counts. Use this whenever the "
            "operator drops a video link and asks 'what is this', "
            "'summarize', 'is this useful', or 'could we implement "
            "this'. Returns plain-English analysis with any "
            "actionable ideas called out. Auto-allowed (NET_READ): "
            "no approval prompt. The downloaded file is deleted "
            "immediately after the analysis returns."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "Public video URL. Must be http(s)://. "
                        "Private posts won't work."
                    ),
                },
                "question": {
                    "type": "string",
                    "description": (
                        "Optional — what specifically the operator "
                        "wants to know. Defaults to 'summarize + flag "
                        "anything worth implementing'."
                    ),
                },
                "n_frames": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_FRAMES,
                    "description": (
                        "Optional — pin a specific frame count. "
                        "Default 0 means 'auto: 1 fps with a "
                        f"{MAX_FRAMES}-frame ceiling for very long "
                        "clips'. Override only if you want denser "
                        "or sparser sampling than that."
                    ),
                },
            },
            "required": ["url"],
        },
        risk=RiskClass.NET_READ,
        handler=_handler,
    )


def make_analyze_video_file_tool(
    anthropic_client: AsyncAnthropic,
    *,
    attachment_store: Any,
    openai_api_key: str | None = None,
    analysis_model: str = DEFAULT_ANALYSIS_MODEL,
    duration_prober: DurationProber | None = None,
    frame_extractor: FrameExtractor | None = None,
    audio_extractor: AudioExtractor | None = None,
    transcriber: Transcriber | None = None,
    analyzer: VideoAnalyzer | None = None,
) -> Tool:
    """Sibling of ``make_analyze_video_url_tool`` that runs the same
    frame + audio + Claude pipeline against an UPLOADED video file
    (Telegram bridge stash, web chat upload). Bypasses yt-dlp; the
    file already exists locally in the attachment store.

    The operator's mental model: "I downloaded the Reel because IG
    won't let you watch it through the API → I drop it on Telegram
    → PILK watches it." That flow needs THIS tool.
    """
    probe_duration = duration_prober or _default_probe_duration
    frames = frame_extractor or _default_extract_frames
    audio = audio_extractor or _default_extract_audio
    api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
    transcribe = transcriber or _make_default_transcriber(api_key)
    analyze = analyzer or _make_default_analyzer(
        anthropic_client, analysis_model,
    )

    async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
        attachment_id = str(args.get("attachment_id") or "").strip()
        if not attachment_id:
            return ToolOutcome(
                content=(
                    "analyze_video_file needs an 'attachment_id'. "
                    "When the operator uploads a video to Telegram "
                    "or chat, the bridge surfaces the id in your "
                    "context — pass that id here."
                ),
                is_error=True,
            )
        if attachment_store is None:
            return ToolOutcome(
                content=(
                    "Attachment store isn't wired on this daemon — "
                    "uploads can't be analyzed yet."
                ),
                is_error=True,
            )
        # Locate the uploaded file. The store exposes ``resolve_many``
        # which returns Attachment objects with their on-disk path.
        try:
            resolved = attachment_store.resolve_many([attachment_id])
        except Exception as e:  # pragma: no cover — defensive
            return ToolOutcome(
                content=(
                    f"Couldn't find attachment {attachment_id}: "
                    f"{type(e).__name__}: {e}"
                ),
                is_error=True,
            )
        if not resolved:
            return ToolOutcome(
                content=(
                    f"Attachment {attachment_id} not found. The file "
                    "may have been cleaned up after the chat session "
                    "ended — ask the operator to re-upload."
                ),
                is_error=True,
            )
        attachment = resolved[0]
        if attachment.kind != "video":
            return ToolOutcome(
                content=(
                    f"Attachment {attachment_id} is a "
                    f"{attachment.kind}, not a video. "
                    "analyze_video_file only handles uploaded "
                    "video files (mp4 / mov / webm)."
                ),
                is_error=True,
            )
        video_path = attachment.path
        if not video_path.is_file():
            return ToolOutcome(
                content=(
                    f"Attachment {attachment_id} is missing on "
                    f"disk at {video_path}. Re-upload and try again."
                ),
                is_error=True,
            )

        question = str(args.get("question") or DEFAULT_QUESTION).strip()
        raw_frames = args.get("n_frames")
        if raw_frames is None:
            n_frames = DEFAULT_FRAMES
        else:
            try:
                n_frames = int(raw_frames)
            except (TypeError, ValueError):
                n_frames = DEFAULT_FRAMES
            n_frames = (
                max(1, min(n_frames, MAX_FRAMES))
                if n_frames > 0
                else DEFAULT_FRAMES
            )

        # Frames + audio extraction need their own tmpdir; the source
        # video itself stays in the attachment store (no copy needed,
        # ffmpeg reads it directly).
        tmpdir = Path(tempfile.mkdtemp(prefix="pilk-video-file-"))
        try:
            try:
                duration_s = await probe_duration(video_path)
            except Exception:
                log.exception(
                    "video_file_duration_probe_failed",
                    attachment_id=attachment_id,
                )
                duration_s = 0.0
            if duration_s > MAX_VIDEO_SECONDS:
                return ToolOutcome(
                    content=(
                        f"Uploaded video is {duration_s:.0f}s long, "
                        f"over the {MAX_VIDEO_SECONDS}s ({MAX_VIDEO_SECONDS // 60}-minute) "
                        "cap for analysis. Trim it down or chunk it "
                        "before re-uploading."
                    ),
                    is_error=True,
                )
            frames_dir = tmpdir / "frames"
            try:
                frame_paths = await frames(
                    video_path, frames_dir, n_frames, duration_s,
                )
            except Exception as e:
                log.exception(
                    "video_file_frame_extract_failed",
                    attachment_id=attachment_id,
                )
                return ToolOutcome(
                    content=(
                        f"Couldn't extract frames from the uploaded "
                        f"video. {type(e).__name__}: {e}. The file "
                        "may be corrupted or use an unsupported codec."
                    ),
                    is_error=True,
                )
            if not frame_paths:
                return ToolOutcome(
                    content=(
                        "ffmpeg returned zero frames — the upload may "
                        "be corrupted or have an unsupported codec."
                    ),
                    is_error=True,
                )

            transcript = ""
            try:
                audio_path = await audio(video_path, tmpdir)
                if audio_path is not None and api_key:
                    transcript = await transcribe(audio_path)
            except Exception:
                log.exception(
                    "video_file_audio_pipeline_failed",
                    attachment_id=attachment_id,
                )
                transcript = ""

            try:
                analysis = await analyze(
                    frame_paths, transcript, question,
                )
            except Exception as e:
                log.exception(
                    "video_file_analyze_call_failed",
                    attachment_id=attachment_id,
                )
                return ToolOutcome(
                    content=(
                        f"Frames were extracted but the analysis "
                        f"call failed: {type(e).__name__}: {e}."
                    ),
                    is_error=True,
                )

            log.info(
                "video_file_analyze_completed",
                attachment_id=attachment_id,
                filename=attachment.filename,
                frames=len(frame_paths),
                transcript_chars=len(transcript),
                duration_s=duration_s,
                model=analysis_model,
            )
            return ToolOutcome(
                content=analysis,
                data={
                    "attachment_id": attachment_id,
                    "filename": attachment.filename,
                    "frames_used": len(frame_paths),
                    "transcript_chars": len(transcript),
                    "model": analysis_model,
                    "had_audio": bool(transcript),
                    "duration_s": duration_s,
                },
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return Tool(
        name="analyze_video_file",
        description=(
            "Watch and analyze a video the operator UPLOADED (via "
            "Telegram, web chat, or any attachment surface). Use this "
            "whenever the inbound message includes an 'attachment_id' "
            "for a video file — typically because the operator hit a "
            "login-walled platform (Instagram Reels, gated TikToks, "
            "paid courses, private DMs) and downloaded the clip "
            "manually before sending it. Same pipeline as "
            "``analyze_video_url`` (1 fps frame sampling, Whisper "
            "transcript, multimodal Claude vision call) but skips the "
            "yt-dlp download step since the file is already local. "
            "Auto-allowed: the file is in PILK's own temp store and "
            "gets cleaned with the rest of the chat session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "attachment_id": {
                    "type": "string",
                    "description": (
                        "The attachment id surfaced by the bridge "
                        "when the operator uploaded the video. "
                        "Look for it in the inbound message's "
                        "context (e.g. '[Uploaded video, "
                        "attachment id abc123…]')."
                    ),
                },
                "question": {
                    "type": "string",
                    "description": (
                        "Optional — what specifically the operator "
                        "wants to know about this video. Defaults to "
                        "'summarize + flag actionable ideas'."
                    ),
                },
                "n_frames": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_FRAMES,
                    "description": (
                        "Optional — pin a specific frame count. "
                        "Default 0 means 'auto: 1 fps with a "
                        f"{MAX_FRAMES}-frame ceiling for longer "
                        "clips'."
                    ),
                },
            },
            "required": ["attachment_id"],
        },
        risk=RiskClass.READ,
        handler=_handler,
    )
