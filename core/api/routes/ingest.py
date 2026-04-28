"""HTTP surface for the file ingestion pipeline.

  POST  /ingest/file               multipart upload — process now
  GET   /ingest                    list recent ingestion runs
  GET   /ingest/{id}               single ingestion row
  POST  /ingest/scan-inbox         operator-pulled scan of ~/PILK/inbox/
  GET   /ingest/supported          list supported file extensions

Operator-pulled by design — there's no daemon polling the inbox
unless explicitly enabled in a future batch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile

from core.ingest import (
    IngestPipeline,
    IngestRow,
    supported_extensions,
)
from core.logging import get_logger

log = get_logger("pilkd.routes.ingest")
router = APIRouter(prefix="/ingest")


def _pipeline(request: Request) -> IngestPipeline:
    p = getattr(request.app.state, "ingest_pipeline", None)
    if p is None:
        raise HTTPException(503, "ingest pipeline not initialised")
    return p


def _registry(request: Request):
    r = getattr(request.app.state, "ingest_registry", None)
    if r is None:
        raise HTTPException(503, "ingest registry not initialised")
    return r


def _inbox_dir(request: Request) -> Path:
    p = getattr(request.app.state, "ingest_inbox_dir", None)
    if p is None:
        raise HTTPException(503, "ingest inbox not configured")
    return p


def _row_to_dict(r: IngestRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "original_path": r.original_path,
        "stored_path": r.stored_path,
        "file_type": r.file_type,
        "project_slug": r.project_slug,
        "content_hash": r.content_hash,
        "byte_size": r.byte_size,
        "status": r.status,
        "extracted_text_path": r.extracted_text_path,
        "brain_note_path": r.brain_note_path,
        "summary": r.summary,
        "error": r.error,
        "metadata": r.metadata,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


@router.get("/supported")
async def list_supported() -> dict[str, Any]:
    return {"extensions": supported_extensions()}


@router.get("")
async def list_recent(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    rows = await _registry(request).list_recent(
        limit=limit, status=status,
    )
    return {
        "files": [_row_to_dict(r) for r in rows],
        "count": len(rows),
    }


@router.get("/{file_id}")
async def get_one(request: Request, file_id: str) -> dict[str, Any]:
    row = await _registry(request).get(file_id)
    if row is None:
        raise HTTPException(404, "not found")
    return _row_to_dict(row)


@router.post("/file")
async def ingest_file(
    request: Request,
    upload: UploadFile = File(...),
    project_slug: str | None = Form(default=None),
) -> dict[str, Any]:
    """Upload a file via multipart, run it through the pipeline,
    and return the resulting row."""
    inbox = _inbox_dir(request)
    inbox.mkdir(parents=True, exist_ok=True)
    safe_name = upload.filename or "upload"
    target = inbox / safe_name
    # Suffix the target if a same-named file is already there.
    i = 1
    while target.exists():
        stem, suf = Path(safe_name).stem, Path(safe_name).suffix
        target = inbox / f"{stem}-{i}{suf}"
        i += 1
    body = await upload.read()
    target.write_bytes(body)
    pipeline = _pipeline(request)
    try:
        result = await pipeline.ingest_path(
            target, project_slug=project_slug or None,
        )
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "row": _row_to_dict(result.row),
        "duplicate": result.duplicate,
        "error": result.error,
        "brain_note_path": result.brain_note_path,
    }


@router.post("/scan-inbox")
async def scan_inbox(
    request: Request,
    project_slug: str | None = Query(default=None),
) -> dict[str, Any]:
    """One-shot scan of ``~/PILK/inbox/`` — process every plain file
    currently sitting there. Skips the ``archive/`` and ``failed/``
    subdirs the pipeline manages itself.

    Returns one row per file processed (or skipped as duplicate)."""
    inbox = _inbox_dir(request)
    if not inbox.exists():
        return {"processed": [], "skipped_dirs": []}
    pipeline = _pipeline(request)
    processed: list[dict[str, Any]] = []
    for entry in sorted(inbox.iterdir()):
        if entry.is_dir():
            continue  # skip archive/ + failed/
        if entry.name.startswith("."):
            continue  # hidden / temp files
        try:
            res = await pipeline.ingest_path(
                entry,
                project_slug=project_slug or None,
            )
            processed.append(
                {
                    "file": entry.name,
                    "id": res.row.id,
                    "status": res.row.status,
                    "duplicate": res.duplicate,
                    "brain_note_path": res.brain_note_path,
                    "error": res.error,
                }
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ingest_scan_file_failed",
                file=str(entry), error=str(e),
            )
            processed.append(
                {
                    "file": entry.name,
                    "id": None,
                    "status": "failed",
                    "duplicate": False,
                    "brain_note_path": None,
                    "error": str(e),
                }
            )
    return {"processed": processed, "count": len(processed)}
