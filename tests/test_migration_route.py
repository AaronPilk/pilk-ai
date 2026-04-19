"""Auth + control-flow tests for POST /migration/upload and GET
/migration/status.

We don't exercise the full FastAPI app here — just the handlers
directly with a stubbed Request, so the assertions stay focused on
who-can-do-what rather than on FastAPI multipart plumbing (which is
covered upstream + would pull in starlette test machinery)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import HTTPException

from core.api.routes import migration as migration_route
from core.db.migrations import ensure_schema
from core.migration import build_bundle


@dataclass
class _FakeAuth:
    user_id: str
    email: str | None


class _FakeState:
    def __init__(self, auth: _FakeAuth | None = None) -> None:
        self.auth = auth


class _FakeApp:
    state: object = None


class _FakeRequest:
    def __init__(self, email: str | None) -> None:
        self.state = _FakeState(
            _FakeAuth(user_id="u-test", email=email)
            if email is not None
            else None
        )
        self.app = _FakeApp()


@pytest.fixture(autouse=True)
def _reset_last_report():
    migration_route._last_report = None
    yield
    migration_route._last_report = None


@pytest.fixture
def master_env(monkeypatch: pytest.MonkeyPatch):
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "supabase_master_admin_email",
        "owner@pilk.ai",
        raising=False,
    )


def test_require_master_admin_accepts_matching_email(master_env) -> None:
    req = _FakeRequest(email="owner@pilk.ai")
    # Does not raise.
    assert migration_route._require_master_admin(req) == "owner@pilk.ai"


def test_require_master_admin_is_case_insensitive(master_env) -> None:
    req = _FakeRequest(email="OWNER@Pilk.AI")
    assert migration_route._require_master_admin(req) == "OWNER@Pilk.AI"


def test_require_master_admin_rejects_mismatch(master_env) -> None:
    req = _FakeRequest(email="intruder@example.com")
    with pytest.raises(HTTPException) as ei:
        migration_route._require_master_admin(req)
    assert ei.value.status_code == 403
    assert "master admin" in ei.value.detail.lower()


def test_require_master_admin_rejects_missing_email(master_env) -> None:
    req = _FakeRequest(email=None)
    with pytest.raises(HTTPException) as ei:
        migration_route._require_master_admin(req)
    assert ei.value.status_code == 403


def test_require_master_admin_disabled_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(
        settings, "supabase_master_admin_email", None, raising=False
    )
    req = _FakeRequest(email="anyone@example.com")
    with pytest.raises(HTTPException) as ei:
        migration_route._require_master_admin(req)
    assert ei.value.status_code == 501


# ── status endpoint behavior ───────────────────────────────────


@pytest.mark.asyncio
async def test_status_returns_none_initially(master_env) -> None:
    req = _FakeRequest(email="owner@pilk.ai")
    body = await migration_route.migration_status(req)
    assert body["last_import"] is None


@pytest.mark.asyncio
async def test_status_returns_report_after_import(master_env) -> None:
    from core.migration import ImportReport

    migration_route._last_report = ImportReport(
        ok=True,
        files_written=4,
        bytes_written=12345,
        backup_path="/tmp/backup-xyz",
    )
    req = _FakeRequest(email="owner@pilk.ai")
    body = await migration_route.migration_status(req)
    assert body["last_import"]["ok"] is True
    assert body["last_import"]["files_written"] == 4


@pytest.mark.asyncio
async def test_status_admin_gated(master_env) -> None:
    req = _FakeRequest(email="random@example.com")
    with pytest.raises(HTTPException) as ei:
        await migration_route.migration_status(req)
    assert ei.value.status_code == 403


# ── upload endpoint basics ─────────────────────────────────────


class _FakeUpload:
    """Minimal UploadFile stand-in — the handler only calls .read()."""

    def __init__(self, content: bytes) -> None:
        self._content = content

    async def read(self) -> bytes:
        return self._content


@pytest.mark.asyncio
async def test_upload_requires_admin(master_env) -> None:
    req = _FakeRequest(email="nope@example.com")
    with pytest.raises(HTTPException) as ei:
        await migration_route.upload_bundle(
            req, bundle=_FakeUpload(b"bytes"), confirm="MIGRATE"
        )
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_upload_requires_confirm_phrase(master_env) -> None:
    req = _FakeRequest(email="owner@pilk.ai")
    with pytest.raises(HTTPException) as ei:
        await migration_route.upload_bundle(
            req, bundle=_FakeUpload(b"bytes"), confirm="sure"
        )
    assert ei.value.status_code == 400
    assert "MIGRATE" in ei.value.detail


@pytest.mark.asyncio
async def test_upload_rejects_empty_body(master_env) -> None:
    req = _FakeRequest(email="owner@pilk.ai")
    with pytest.raises(HTTPException) as ei:
        await migration_route.upload_bundle(
            req, bundle=_FakeUpload(b""), confirm="MIGRATE"
        )
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_applies_valid_bundle(
    master_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Build a real bundle from a minimal seed home.
    src = tmp_path / "src"
    src.mkdir()
    ensure_schema(src / "pilk.db")
    bundle_path = tmp_path / "bundle.zip"
    build_bundle(home=src, output_path=bundle_path)

    # Point the handler at a brand-new target home.
    target = tmp_path / "target"
    target.mkdir()
    ensure_schema(target / "pilk.db")

    from core.config import get_settings

    settings = get_settings()
    # ``home`` is a real Settings field; setting it updates what
    # ``resolve_home()`` returns without poking at the frozen method.
    monkeypatch.setattr(settings, "home", target)

    req = _FakeRequest(email="owner@pilk.ai")
    body = await migration_route.upload_bundle(
        req,
        bundle=_FakeUpload(bundle_path.read_bytes()),
        confirm="MIGRATE",
    )
    assert body["ok"] is True
    assert body["files_written"] >= 1
    assert body["next_step"] is not None
    assert "Redeploy" in body["next_step"]
