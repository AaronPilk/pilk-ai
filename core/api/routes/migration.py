"""HTTP surface for local → cloud migration.

  POST /migration/upload   — multipart upload of a bundle zip
                              + confirm phrase. Master-admin only.
  GET  /migration/status   — last migration result summary.

Every write path here is gated on ``settings.supabase_master_admin_email``
matching the authenticated caller's email. This isn't a routine
operation — losing the gate means anyone with a valid Supabase token
could overwrite cloud state.

The upload path **does not restart pilkd**. Active stores hold
references to pre-migration file descriptors, so the operator sees
a "restart pilkd to pick up imported data" note in the response. The
Railway "Redeploy" button is the cleanest way to apply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.params import File, Form

from core.config import get_settings
from core.logging import get_logger
from core.migration import (
    MAX_BUNDLE_SIZE_BYTES,
    ImportReport,
    apply_bundle,
)

log = get_logger("pilkd.migration")

router = APIRouter(prefix="/migration")

# Last import outcome lives in memory on this process. Small — a
# single ImportReport. Survives until restart (which is what an
# import triggers by design).
_last_report: ImportReport | None = None


def _require_master_admin(request: Request) -> str:
    """Return the caller's email iff they're the master admin.
    Raises HTTPException(403) on any mismatch."""
    settings = get_settings()
    master = settings.supabase_master_admin_email
    if not master:
        raise HTTPException(
            status_code=501,
            detail=(
                "Migration is disabled: no supabase_master_admin_email "
                "configured on the server."
            ),
        )
    auth = getattr(request.state, "auth", None)
    email = getattr(auth, "email", None)
    if not email:
        raise HTTPException(
            status_code=403,
            detail="Migration requires an authenticated caller with an email claim.",
        )
    if email.lower().strip() != master.lower().strip():
        raise HTTPException(
            status_code=403,
            detail="Migration is restricted to the master admin account.",
        )
    return email


@router.post("/upload")
async def upload_bundle(
    request: Request,
    bundle: UploadFile = File(...),  # noqa: B008 — FastAPI dependency pattern
    confirm: str = Form(...),
) -> dict[str, Any]:
    """Apply a migration bundle to the current pilkd home.

    Multipart body:
      * ``bundle``: the zip file
      * ``confirm``: must be the literal string ``"MIGRATE"``. Guards
        against accidental curl invocations with a stale bundle.
    """
    email = _require_master_admin(request)
    if confirm != "MIGRATE":
        raise HTTPException(
            status_code=400,
            detail="confirm must be the literal string 'MIGRATE'.",
        )

    bundle_bytes = await bundle.read()
    if not bundle_bytes:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(bundle_bytes) > MAX_BUNDLE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"bundle exceeds {MAX_BUNDLE_SIZE_BYTES:,}-byte limit",
        )

    settings = get_settings()
    target_home = settings.resolve_home()
    # Repo-root clients/ directory; same convention as FastAPI lifespan.
    target_clients = Path(__file__).resolve().parents[3] / "clients"

    log.info(
        "migration_upload_start",
        by=email,
        bytes=len(bundle_bytes),
        target_home=str(target_home),
    )

    report = apply_bundle(
        bundle_bytes=bundle_bytes,
        target_home=target_home,
        target_clients_dir=target_clients,
    )

    global _last_report
    _last_report = report

    if report.ok:
        log.info(
            "migration_upload_ok",
            by=email,
            files=report.files_written,
            bytes=report.bytes_written,
            backup=report.backup_path,
        )
    else:
        log.warning("migration_upload_failed", by=email, error=report.error)

    return {
        "ok": report.ok,
        "files_written": report.files_written,
        "bytes_written": report.bytes_written,
        "backup_path": report.backup_path,
        "manifest": report.manifest,
        "warnings": report.warnings,
        "error": report.error,
        # Operator-facing instruction: without a restart, in-memory
        # stores still reference the pre-migration files.
        "next_step": (
            "Redeploy the pilkd process (Railway → Redeploy) so the "
            "imported data is loaded into memory. Your current session "
            "may see stale state until the restart completes."
            if report.ok
            else None
        ),
    }


@router.get("/status")
async def migration_status(request: Request) -> dict[str, Any]:
    """Return the outcome of the last migration on this pilkd
    instance. Admin-gated like the upload endpoint — no point
    leaking file paths to unauthenticated callers."""
    _require_master_admin(request)
    if _last_report is None:
        return {
            "last_import": None,
            "note": "No migration has been applied on this pilkd process.",
        }
    return {
        "last_import": {
            "ok": _last_report.ok,
            "files_written": _last_report.files_written,
            "bytes_written": _last_report.bytes_written,
            "backup_path": _last_report.backup_path,
            "manifest": _last_report.manifest,
            "warnings": _last_report.warnings,
            "error": _last_report.error,
        }
    }
