"""Multipart upload surface for chat attachments.

  POST /chat/uploads    multipart `file` → {attachments: [{id, kind, ...}]}
  DELETE /chat/uploads/{id}   drop one (used when the operator removes a
                              preview before sending)

Stored files live under `$PILK_HOME/temp/chat-uploads/{id}.bin` plus a
paired `.json` metadata file. The orchestrator reads them off disk when
it composes the first turn of a chat plan; see
`core/chat/attachments.py` for the store.

Scope is intentionally narrow:
  * images (png/jpeg/gif/webp) — routed to Anthropic vision blocks
  * documents (pdf) — routed to Anthropic document blocks
  * text (txt/md/csv/json) — inlined as extra text blocks

Anything else is rejected at the door so the browser sees a 415 instead
of a mystery failure deeper in the pipeline.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from core.chat import AttachmentError, AttachmentStore, is_allowed_mime
from core.logging import get_logger

router = APIRouter(prefix="/chat")
log = get_logger("pilkd.routes.chat_uploads")


def _store(request: Request) -> AttachmentStore:
    store = getattr(request.app.state, "chat_attachments", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="chat attachment store offline"
        )
    return store


@router.post("/uploads")
async def upload_chat_attachment(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008 — FastAPI dependency pattern
) -> dict[str, Any]:
    store = _store(request)
    raw_mime = file.content_type or "application/octet-stream"
    if not is_allowed_mime(raw_mime):
        raise HTTPException(
            status_code=415,
            detail=(
                f"unsupported attachment type: {raw_mime}. "
                "Accepted: images (png/jpeg/gif/webp), PDFs, "
                "text (txt/md/csv/json)."
            ),
        )
    payload = await file.read()
    try:
        att = store.save(
            payload=payload,
            mime=raw_mime,
            filename=file.filename or "attachment",
        )
    except AttachmentError as e:
        # MIME + size guards live in the store; translate to 400/415.
        msg = str(e)
        status = 413 if "too large" in msg else 400
        raise HTTPException(status_code=status, detail=msg) from e
    return {"attachment": att.public_dict()}


@router.delete("/uploads/{attachment_id}")
async def delete_chat_attachment(
    attachment_id: str, request: Request
) -> dict[str, Any]:
    store = _store(request)
    removed = store.remove(attachment_id)
    return {"id": attachment_id, "removed": removed}
