"""Tests for ``analyze_video_url`` — the tool that watches a public
video URL via yt-dlp + ffmpeg + Whisper + Claude vision.

All four pipeline stages have constructor seams (downloader,
frame_extractor, audio_extractor, transcriber, analyzer) so the
test run is fully offline — no real yt-dlp, no real ffmpeg, no
real OpenAI / Anthropic calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.tools.builtin.video_analyze import (
    DEFAULT_FRAMES,
    MAX_FRAMES,
    make_analyze_video_url_tool,
)
from core.tools.registry import ToolContext

# ── Fakes ───────────────────────────────────────────────────────


class _FakeAnthropic:
    """Minimal ``messages.create`` stand-in. Not actually used in
    most tests because we inject our own analyzer — kept here for
    the path that exercises the default analyzer."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        outer = self

        class _Messages:
            async def create(self, **kwargs: Any) -> Any:
                outer.calls.append(kwargs)
                raise AssertionError(
                    "tests should inject 'analyzer' to bypass this"
                )

        self.messages = _Messages()


def _make_video_file(dest_dir: Path, name: str = "video.mp4") -> Path:
    path = dest_dir / name
    path.write_bytes(b"\x00" * 64)
    return path


def _make_frame_file(dest_dir: Path, idx: int) -> Path:
    path = dest_dir / f"frame_{idx:03d}.jpg"
    # Real-ish JPEG-y bytes so any default code path that touched
    # them wouldn't immediately blow up. We never actually render.
    path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 32)
    return path


def _make_seams(
    *,
    n_frames: int = DEFAULT_FRAMES,
    transcript: str = "spoken words from the video",
    analysis: str = "Plain-English analysis of the video.",
    download_fail: bool = False,
    frames_fail: bool = False,
    audio_returns_none: bool = False,
    transcribe_fail: bool = False,
    analyze_fail: bool = False,
):
    """Build the five stage callables with options to simulate
    each failure mode in turn. Returns a dict with the callables +
    a recorder so tests can assert who got called with what."""

    record: dict[str, Any] = {
        "download_calls": [],
        "frames_calls": [],
        "audio_calls": [],
        "transcribe_calls": [],
        "analyze_calls": [],
    }

    async def downloader(url: str, dest_dir: Path) -> Path:
        record["download_calls"].append({"url": url, "dir": dest_dir})
        if download_fail:
            raise RuntimeError("yt-dlp 403 forbidden")
        return _make_video_file(dest_dir)

    async def frame_extractor(
        video_path: Path, frames_dir: Path, count: int,
    ) -> list[Path]:
        record["frames_calls"].append(
            {"video": video_path, "dir": frames_dir, "count": count}
        )
        if frames_fail:
            raise RuntimeError("ffmpeg 1 unsupported codec")
        frames_dir.mkdir(parents=True, exist_ok=True)
        return [_make_frame_file(frames_dir, i) for i in range(count)]

    async def audio_extractor(
        video_path: Path, dest_dir: Path,
    ) -> Path | None:
        record["audio_calls"].append(
            {"video": video_path, "dir": dest_dir}
        )
        if audio_returns_none:
            return None
        out = dest_dir / "audio.mp3"
        out.write_bytes(b"\x00" * 4096)
        return out

    async def transcriber(audio_path: Path) -> str:
        record["transcribe_calls"].append(audio_path)
        if transcribe_fail:
            raise RuntimeError("whisper 429 rate limit")
        return transcript

    async def analyzer(
        frame_paths: list[Path],
        seen_transcript: str,
        question: str,
    ) -> str:
        record["analyze_calls"].append(
            {
                "frames": frame_paths,
                "transcript": seen_transcript,
                "question": question,
            }
        )
        if analyze_fail:
            raise RuntimeError("anthropic 500 internal")
        return analysis

    record["downloader"] = downloader
    record["frame_extractor"] = frame_extractor
    record["audio_extractor"] = audio_extractor
    record["transcriber"] = transcriber
    record["analyzer"] = analyzer
    return record


def _make_tool(
    seams: dict[str, Any],
    *,
    openai_api_key: str | None = "sk-test",
):
    return make_analyze_video_url_tool(
        anthropic_client=_FakeAnthropic(),
        openai_api_key=openai_api_key,
        downloader=seams["downloader"],
        frame_extractor=seams["frame_extractor"],
        audio_extractor=seams["audio_extractor"],
        transcriber=seams["transcriber"],
        analyzer=seams["analyzer"],
    )


# ── Validation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_url_returns_error() -> None:
    seams = _make_seams()
    tool = _make_tool(seams)
    out = await tool.handler({}, ToolContext())
    assert out.is_error
    assert "url" in out.content.lower()
    assert seams["download_calls"] == []


@pytest.mark.asyncio
async def test_non_http_url_rejected() -> None:
    seams = _make_seams()
    tool = _make_tool(seams)
    out = await tool.handler(
        {"url": "ftp://example.com/clip.mp4"}, ToolContext(),
    )
    assert out.is_error
    assert "http" in out.content.lower()
    assert seams["download_calls"] == []


# ── Happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_full_pipeline() -> None:
    seams = _make_seams()
    tool = _make_tool(seams)
    out = await tool.handler(
        {"url": "https://www.tiktok.com/@x/video/123"},
        ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.content == "Plain-English analysis of the video."
    assert out.data["url"] == (
        "https://www.tiktok.com/@x/video/123"
    )
    assert out.data["frames_used"] == DEFAULT_FRAMES
    assert out.data["had_audio"] is True
    assert out.data["transcript_chars"] > 0
    # All five stages fired once.
    assert len(seams["download_calls"]) == 1
    assert len(seams["frames_calls"]) == 1
    assert len(seams["audio_calls"]) == 1
    assert len(seams["transcribe_calls"]) == 1
    assert len(seams["analyze_calls"]) == 1
    # Analyzer received the transcript and the operator's
    # default question.
    analyze_call = seams["analyze_calls"][0]
    assert analyze_call["transcript"] == "spoken words from the video"
    assert "summarize" in analyze_call["question"].lower()


@pytest.mark.asyncio
async def test_explicit_question_passes_through() -> None:
    seams = _make_seams()
    tool = _make_tool(seams)
    custom_q = "Could we add this scrolling pattern to PILK's UI?"
    out = await tool.handler(
        {
            "url": "https://www.instagram.com/reel/abc/",
            "question": custom_q,
        },
        ToolContext(),
    )
    assert not out.is_error
    analyze_call = seams["analyze_calls"][0]
    assert analyze_call["question"] == custom_q


@pytest.mark.asyncio
async def test_n_frames_clamped_to_max() -> None:
    seams = _make_seams()
    tool = _make_tool(seams)
    out = await tool.handler(
        {"url": "https://x.com/i/status/1", "n_frames": 999},
        ToolContext(),
    )
    assert not out.is_error
    # Frame extractor was asked for the cap, not 999.
    assert seams["frames_calls"][0]["count"] == MAX_FRAMES


# ── Tmp-dir cleanup ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tmpdir_is_cleaned_after_success() -> None:
    seams = _make_seams()
    tool = _make_tool(seams)
    out = await tool.handler(
        {"url": "https://example.com/v"}, ToolContext(),
    )
    assert not out.is_error
    download_dir: Path = seams["download_calls"][0]["dir"]
    assert not download_dir.exists()


@pytest.mark.asyncio
async def test_tmpdir_is_cleaned_after_failure() -> None:
    """Even when a stage explodes, the tmp dir must not leak."""
    seams = _make_seams(analyze_fail=True)
    tool = _make_tool(seams)
    out = await tool.handler(
        {"url": "https://example.com/v"}, ToolContext(),
    )
    assert out.is_error
    download_dir: Path = seams["download_calls"][0]["dir"]
    assert not download_dir.exists()


# ── Failure-mode messages ──────────────────────────────────────


@pytest.mark.asyncio
async def test_download_failure_returns_clean_message() -> None:
    seams = _make_seams(download_fail=True)
    tool = _make_tool(seams)
    out = await tool.handler(
        {"url": "https://insta.gram/private"}, ToolContext(),
    )
    assert out.is_error
    assert "private" in out.content.lower() or "10-minute" in out.content
    # Subsequent stages never ran.
    assert seams["frames_calls"] == []
    assert seams["analyze_calls"] == []


@pytest.mark.asyncio
async def test_frame_extraction_failure() -> None:
    seams = _make_seams(frames_fail=True)
    tool = _make_tool(seams)
    out = await tool.handler(
        {"url": "https://x.com/v"}, ToolContext(),
    )
    assert out.is_error
    assert "frames" in out.content.lower()
    assert seams["analyze_calls"] == []


@pytest.mark.asyncio
async def test_silent_video_still_analyzed() -> None:
    """No-audio clips (some Instagram silent reels) should still
    produce an analysis — visuals carry the run."""
    seams = _make_seams(audio_returns_none=True)
    tool = _make_tool(seams)
    out = await tool.handler(
        {"url": "https://insta.gram/silent"}, ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["had_audio"] is False
    assert out.data["transcript_chars"] == 0
    # Transcriber never invoked when audio extractor returned None.
    assert seams["transcribe_calls"] == []
    # Analyzer still fired with empty transcript.
    assert len(seams["analyze_calls"]) == 1
    assert seams["analyze_calls"][0]["transcript"] == ""


@pytest.mark.asyncio
async def test_transcription_failure_falls_back_to_visuals() -> None:
    """Whisper failure shouldn't kill the run — analyze frames
    only and report no transcript."""
    seams = _make_seams(transcribe_fail=True)
    tool = _make_tool(seams)
    out = await tool.handler(
        {"url": "https://x.com/v"}, ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["had_audio"] is False
    assert out.data["transcript_chars"] == 0
    assert seams["analyze_calls"][0]["transcript"] == ""


@pytest.mark.asyncio
async def test_no_openai_key_skips_transcription() -> None:
    """Without an OpenAI key, the tool runs visuals-only and
    never calls the transcriber even if audio is present."""
    seams = _make_seams()
    tool = _make_tool(seams, openai_api_key=None)
    out = await tool.handler(
        {"url": "https://x.com/v"}, ToolContext(),
    )
    assert not out.is_error, out.content
    assert out.data["had_audio"] is False
    assert seams["transcribe_calls"] == []


# ── Surface ─────────────────────────────────────────────────────


def test_tool_surface() -> None:
    seams = _make_seams()
    tool = _make_tool(seams)
    assert tool.name == "analyze_video_url"
    assert tool.input_schema["required"] == ["url"]
    props = tool.input_schema["properties"]
    assert "question" in props
    assert "n_frames" in props
    assert props["n_frames"]["maximum"] == MAX_FRAMES
