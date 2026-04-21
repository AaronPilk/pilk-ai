"""Attachment store + orchestrator content-block composition.

The store is disk-backed; we point it at tmp_path for isolation. The
orchestrator helper ``_build_user_content`` is exercised directly so
we don't need to spin up a full stubbed Anthropic roundtrip — it's a
pure function of (goal, attachments).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.chat.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentError,
    AttachmentStore,
    attachment_kind_from_mime,
    is_allowed_mime,
)
from core.orchestrator.orchestrator import ChatAttachment, _build_user_content

# ── MIME classification ─────────────────────────────────────────────


def test_is_allowed_mime_accepts_supported_types() -> None:
    for mime in (
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
    ):
        assert is_allowed_mime(mime), mime


def test_is_allowed_mime_strips_charset_parameters() -> None:
    assert is_allowed_mime("text/plain; charset=utf-8") is True
    assert is_allowed_mime("image/PNG") is True  # case-insensitive


def test_is_allowed_mime_rejects_random_types() -> None:
    assert is_allowed_mime("application/zip") is False
    assert is_allowed_mime("video/mp4") is False
    assert is_allowed_mime("") is False


def test_attachment_kind_from_mime_maps_correctly() -> None:
    assert attachment_kind_from_mime("image/png") == "image"
    assert attachment_kind_from_mime("application/pdf") == "document"
    assert attachment_kind_from_mime("text/markdown") == "text"


def test_attachment_kind_from_mime_rejects_unknown() -> None:
    with pytest.raises(AttachmentError):
        attachment_kind_from_mime("application/octet-stream")


# ── AttachmentStore round-trip ──────────────────────────────────────


def test_save_and_get_roundtrip(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    store.ensure_layout()
    att = store.save(
        payload=b"hello",
        mime="text/plain",
        filename="notes.txt",
    )
    assert att.kind == "text"
    assert att.size == 5
    assert att.filename == "notes.txt"
    assert att.path.read_bytes() == b"hello"

    got = store.get(att.id)
    assert got is not None
    assert got.id == att.id
    assert got.mime == "text/plain"
    assert got.path == att.path


def test_save_rejects_empty_payload(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    with pytest.raises(AttachmentError, match="empty upload"):
        store.save(payload=b"", mime="text/plain", filename="n.txt")


def test_save_rejects_oversize_payload(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    with pytest.raises(AttachmentError, match="too large"):
        store.save(
            payload=b"x" * (MAX_ATTACHMENT_BYTES + 1),
            mime="image/png",
            filename="big.png",
        )


def test_save_rejects_disallowed_mime(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    with pytest.raises(AttachmentError, match="unsupported mime"):
        store.save(
            payload=b"ZIP bytes here",
            mime="application/zip",
            filename="secrets.zip",
        )


def test_save_sanitises_traversal_filename(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    att = store.save(
        payload=b"x",
        mime="text/plain",
        filename="../../etc/passwd",
    )
    # Path-separators scrubbed, ``..`` neutralised — filename is a
    # display label only, never a filesystem path.
    assert "/" not in att.filename
    assert "\\" not in att.filename
    assert ".." not in att.filename


def test_resolve_many_raises_on_unknown(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    store.ensure_layout()
    with pytest.raises(AttachmentError, match="unknown attachment"):
        store.resolve_many(["does-not-exist"])


def test_remove_deletes_both_files(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    att = store.save(payload=b"x", mime="text/plain", filename="a.txt")
    assert store.remove(att.id) is True
    assert store.get(att.id) is None
    assert store.remove(att.id) is False


# ── Orchestrator content-block composition ─────────────────────────


def _make_attachment(
    tmp_path: Path,
    *,
    kind: str,
    mime: str,
    filename: str,
    payload: bytes,
) -> ChatAttachment:
    path = tmp_path / filename
    path.write_bytes(payload)
    return ChatAttachment(
        id=filename,
        kind=kind,
        mime=mime,
        filename=filename,
        path=path,
    )


def test_build_user_content_plain_goal_stays_string() -> None:
    # Cheap path: no attachments => keep the original "content as str"
    # shape so Anthropic prompt-caching doesn't see a pointless change.
    out = _build_user_content("hello", [])
    assert out == "hello"


def test_build_user_content_image_adds_base64_block(tmp_path: Path) -> None:
    att = _make_attachment(
        tmp_path,
        kind="image",
        mime="image/png",
        filename="pic.png",
        payload=b"\x89PNG\r\n\x1a\n",
    )
    blocks = _build_user_content("describe this", [att])
    assert isinstance(blocks, list)
    assert blocks[0] == {"type": "text", "text": "describe this"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["type"] == "base64"
    assert blocks[1]["source"]["media_type"] == "image/png"
    assert blocks[1]["source"]["data"]  # non-empty


def test_build_user_content_document_adds_title(tmp_path: Path) -> None:
    att = _make_attachment(
        tmp_path,
        kind="document",
        mime="application/pdf",
        filename="invoice.pdf",
        payload=b"%PDF-1.4\n",
    )
    blocks = _build_user_content("summarise", [att])
    assert isinstance(blocks, list)
    assert blocks[1]["type"] == "document"
    assert blocks[1]["title"] == "invoice.pdf"
    assert blocks[1]["source"]["media_type"] == "application/pdf"


def test_build_user_content_text_inlines_file_contents(tmp_path: Path) -> None:
    att = _make_attachment(
        tmp_path,
        kind="text",
        mime="text/plain",
        filename="notes.txt",
        payload=b"line one\nline two\n",
    )
    blocks = _build_user_content("what's in here?", [att])
    assert isinstance(blocks, list)
    assert blocks[1]["type"] == "text"
    # Filename marker + fenced body so Claude can tell the boundary.
    assert "notes.txt" in blocks[1]["text"]
    assert "line one" in blocks[1]["text"]
    assert "line two" in blocks[1]["text"]


def test_build_user_content_skips_unreadable_file(tmp_path: Path) -> None:
    # Meta points at a path that was already cleaned up; composer
    # logs a warning and leaves the block out instead of crashing
    # the whole plan.
    att = ChatAttachment(
        id="ghost",
        kind="image",
        mime="image/png",
        filename="ghost.png",
        path=tmp_path / "missing.png",
    )
    blocks = _build_user_content("go", [att])
    assert isinstance(blocks, list)
    assert blocks == [{"type": "text", "text": "go"}]
