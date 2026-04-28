"""Disk-backed store for user-uploaded chat attachments.

The chat surface needs a landing strip for files the operator drops
into the composer — images for vision, PDFs for document Q&A, plain
text for inline reference. We keep the design boringly simple: each
attachment is a pair of files under ``$PILK_HOME/temp/chat-uploads``:

    {id}.json    → metadata (kind, mime, filename, size, created_at)
    {id}.bin     → raw bytes

No database rows, no in-memory index. Orchestrator reads attachments
straight off disk when it composes the first turn, so a daemon restart
mid-upload doesn't orphan any state. Cleanup is a standalone pass
(see ``AttachmentStore.purge_older_than``) that the operator can call
from a maintenance task; we don't run it on a timer in this PR.

The classification into ``kind`` matters to the orchestrator — it's
what decides whether to emit a Claude vision block, a document block,
or plain text. Centralising the table here keeps the upload endpoint,
the orchestrator, and the governor all reading the same mapping.
"""

from __future__ import annotations

import contextlib
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from core.logging import get_logger

log = get_logger("pilkd.chat.attachments")

AttachmentKind = Literal["image", "document", "text", "video"]


# Per-kind MIME allowlists. Anthropic's vision/document blocks accept
# these directly; text kinds are base64-independent and are expanded
# into plain text blocks at composition time. Video kinds are stored
# verbatim — they get analyzed via ``analyze_video_file`` (frame
# extraction + Whisper + multimodal Claude), not sent inline to the
# planner.
_IMAGE_MIMES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)
_DOCUMENT_MIMES: frozenset[str] = frozenset({"application/pdf"})
_TEXT_MIMES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        # Some browsers send `text/x-markdown` or empty type for .md.
        "text/x-markdown",
    }
)
# Video MIMEs. Telegram sends video/mp4 by default; iPhone Safari
# uploads video/quicktime for .mov; Android Chrome sends video/webm
# for screen recordings. Anthropic vision can't ingest raw video so
# these never go inline as image blocks — the analyze_video_file
# tool handles them via the same pipeline as analyze_video_url.
_VIDEO_MIMES: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/quicktime",  # .mov from iPhone
        "video/webm",
        "video/x-m4v",
        "video/3gpp",
    }
)

# Hard size cap per non-video file. Videos get a higher ceiling
# below — Anthropic's per-request payload ceiling is enforced at
# the API layer; this stops obvious abuse at the door.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MiB

# Larger ceiling specifically for video uploads. A short Reel /
# TikTok / phone-camera recording is typically 5–80 MiB; this cap
# fits the common case and matches the analyze_video_url tool's
# 300 MiB yt-dlp ceiling so the two paths feel symmetric.
MAX_VIDEO_ATTACHMENT_BYTES = 250 * 1024 * 1024  # 250 MiB


class AttachmentError(ValueError):
    """Raised on MIME / size / missing-file issues."""


def is_allowed_mime(mime: str) -> bool:
    base = (mime or "").split(";", 1)[0].strip().lower()
    return (
        base in _IMAGE_MIMES
        or base in _DOCUMENT_MIMES
        or base in _TEXT_MIMES
        or base in _VIDEO_MIMES
    )


def attachment_kind_from_mime(mime: str) -> AttachmentKind:
    base = (mime or "").split(";", 1)[0].strip().lower()
    if base in _IMAGE_MIMES:
        return "image"
    if base in _DOCUMENT_MIMES:
        return "document"
    if base in _TEXT_MIMES:
        return "text"
    if base in _VIDEO_MIMES:
        return "video"
    raise AttachmentError(f"unsupported attachment mime: {mime!r}")


def size_cap_for_mime(mime: str) -> int:
    """Per-MIME size limit. Video gets a higher ceiling because raw
    short-form video (~30-90s phone recording) routinely runs 30–
    150 MiB. Other kinds use the standard 20 MiB cap."""
    base = (mime or "").split(";", 1)[0].strip().lower()
    if base in _VIDEO_MIMES:
        return MAX_VIDEO_ATTACHMENT_BYTES
    return MAX_ATTACHMENT_BYTES


@dataclass
class Attachment:
    """Stored metadata for one uploaded file.

    ``path`` is where the raw bytes live; the orchestrator reads it
    lazily so a big PDF isn't kept in memory between upload and use.
    """

    id: str
    kind: AttachmentKind
    mime: str
    filename: str
    size: int
    created_at: str
    path: Path

    def public_dict(self) -> dict:
        d = asdict(self)
        d.pop("path", None)  # never send filesystem paths to the browser
        return d


class AttachmentStore:
    """File-backed store under ``$PILK_HOME/temp/chat-uploads``.

    Thread-safety isn't a concern — FastAPI serialises its event-loop
    handlers per endpoint, and distinct uploads always land under
    distinct IDs (``secrets.token_urlsafe``).
    """

    def __init__(self, home: Path) -> None:
        self._root = home / "temp" / "chat-uploads"

    def ensure_layout(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    # ── writes ───────────────────────────────────────────────────

    def save(
        self,
        *,
        payload: bytes,
        mime: str,
        filename: str,
    ) -> Attachment:
        if not payload:
            raise AttachmentError("empty upload")
        base_mime = (mime or "application/octet-stream").split(";", 1)[0].strip().lower()
        cap = size_cap_for_mime(base_mime)
        if len(payload) > cap:
            raise AttachmentError(
                f"attachment too large: {len(payload)} bytes "
                f"(max {cap})"
            )
        if not is_allowed_mime(base_mime):
            raise AttachmentError(f"unsupported mime: {mime!r}")
        kind = attachment_kind_from_mime(base_mime)
        self.ensure_layout()

        aid = secrets.token_urlsafe(16)
        bin_path = self._root / f"{aid}.bin"
        meta_path = self._root / f"{aid}.json"
        tmp_bin = bin_path.with_suffix(".bin.tmp")
        tmp_meta = meta_path.with_suffix(".json.tmp")
        tmp_bin.write_bytes(payload)
        tmp_bin.replace(bin_path)
        att = Attachment(
            id=aid,
            kind=kind,
            mime=base_mime,
            filename=_safe_filename(filename),
            size=len(payload),
            created_at=datetime.now(UTC).isoformat(),
            path=bin_path,
        )
        meta = asdict(att)
        meta["path"] = str(att.path)
        tmp_meta.write_text(json.dumps(meta, indent=2))
        tmp_meta.replace(meta_path)
        with contextlib.suppress(Exception):
            bin_path.chmod(0o600)
            meta_path.chmod(0o600)
        log.info(
            "chat_attachment_saved",
            id=aid,
            kind=kind,
            mime=base_mime,
            size=len(payload),
        )
        return att

    def remove(self, attachment_id: str) -> bool:
        bin_path = self._root / f"{attachment_id}.bin"
        meta_path = self._root / f"{attachment_id}.json"
        removed = False
        for p in (bin_path, meta_path):
            if p.exists():
                with contextlib.suppress(Exception):
                    p.unlink()
                    removed = True
        return removed

    # ── reads ────────────────────────────────────────────────────

    def get(self, attachment_id: str) -> Attachment | None:
        meta_path = self._root / f"{attachment_id}.json"
        if not meta_path.exists():
            return None
        try:
            raw = json.loads(meta_path.read_text())
        except Exception as e:
            log.warning("chat_attachment_meta_unreadable", id=attachment_id, error=str(e))
            return None
        return Attachment(
            id=raw["id"],
            kind=raw["kind"],
            mime=raw["mime"],
            filename=raw["filename"],
            size=int(raw["size"]),
            created_at=raw["created_at"],
            path=Path(raw["path"]),
        )

    def resolve_many(self, ids: list[str]) -> list[Attachment]:
        out: list[Attachment] = []
        for aid in ids:
            a = self.get(aid)
            if a is None:
                raise AttachmentError(f"unknown attachment: {aid}")
            out.append(a)
        return out

    # ── maintenance ──────────────────────────────────────────────

    def purge_older_than(self, seconds: float) -> int:
        """Delete attachments older than `seconds`. Returns the count."""
        if not self._root.exists():
            return 0
        now = datetime.now(UTC).timestamp()
        removed = 0
        for meta_path in self._root.glob("*.json"):
            try:
                mtime = meta_path.stat().st_mtime
            except OSError:
                continue
            if now - mtime < seconds:
                continue
            aid = meta_path.stem
            if self.remove(aid):
                removed += 1
        return removed


def _safe_filename(name: str) -> str:
    """Strip path separators + clamp length. We only use the filename
    for display and for the `filename` hint on the Anthropic document
    block — never as a filesystem path — so aggressive sanitising is
    fine.
    """
    cleaned = (name or "").replace("/", "_").replace("\\", "_").strip()
    # Drop anything that looks like a directory traversal attempt.
    cleaned = cleaned.replace("..", "_")
    return cleaned[:180] or "attachment"
