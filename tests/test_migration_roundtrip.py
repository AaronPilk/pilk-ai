"""Migration round-trip tests — export from one home, import into
another, verify every row + account survives. No network."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.db.migrations import ensure_schema
from core.identity.accounts import AccountsStore, OAuthTokens
from core.migration import (
    BUNDLE_VERSION,
    ImportReport,
    apply_bundle,
    build_bundle,
)
from core.migration.exporter import ExportError
from core.secrets import IntegrationSecretsStore


def _insert_memory(db_path: Path, title: str) -> None:
    """Direct SQL insert so the test helper stays synchronous.
    MemoryStore.add is async + requires a body; we only need the
    minimum row for round-trip verification."""
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO memory_entries(
                   id, kind, title, body, source, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                f"mem-{title.replace(' ', '-')[:20]}",
                "fact",
                title,
                f"body for {title}",
                "user",
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_home(tmp_path: Path, label: str) -> Path:
    """Build a realistic pilkd home with a seeded DB + OAuth tokens."""
    home = tmp_path / label
    home.mkdir()
    ensure_schema(home / "pilk.db")

    _insert_memory(home / "pilk.db", "loves olives")
    _insert_memory(home / "pilk.db", "favorite city: Tampa")

    secrets = IntegrationSecretsStore(home / "pilk.db")
    secrets.upsert("ghl_api_key", "pit-test-12345")
    secrets.upsert("hunter_io_api_key", "hunt-test-12345")

    accounts = AccountsStore(home)
    accounts.ensure_layout()
    accounts.upsert(
        provider="google",
        role="user",
        label="primary",
        email="operator@example.com",
        username="Op",
        scopes=["gmail.readonly"],
        tokens=OAuthTokens(
            access_token="at-test",
            refresh_token="rt-test",
            client_id="cid",
            client_secret="cs",
            scopes=["gmail.readonly"],
        ),
        make_default=True,
    )
    return home


def test_export_creates_zip(tmp_path: Path) -> None:
    home = _make_home(tmp_path, "src")
    output = tmp_path / "bundle.zip"

    manifest = build_bundle(home=home, output_path=output)

    assert output.exists()
    assert output.stat().st_size > 0
    assert manifest.bundle_version == BUNDLE_VERSION
    assert manifest.table_counts.memory_entries == 2
    assert manifest.table_counts.integration_secrets == 2
    assert manifest.account_count == 1


def test_export_refuses_missing_home(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="no SQLite database"):
        build_bundle(home=tmp_path / "nope", output_path=tmp_path / "x.zip")


def test_export_then_import_restores_memory(tmp_path: Path) -> None:
    src = _make_home(tmp_path, "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    ensure_schema(dst / "pilk.db")  # empty target

    bundle = tmp_path / "bundle.zip"
    build_bundle(home=src, output_path=bundle)

    report = apply_bundle(
        bundle_bytes=bundle.read_bytes(),
        target_home=dst,
    )
    assert report.ok, report.error
    assert report.files_written > 0

    # Memory rows must show up in the target. Query directly — the
    # async MemoryStore isn't needed here and would bloat the test.
    conn = sqlite3.connect(dst / "pilk.db")
    try:
        titles = {r[0] for r in conn.execute(
            "SELECT title FROM memory_entries"
        ).fetchall()}
    finally:
        conn.close()
    assert "loves olives" in titles
    assert "favorite city: Tampa" in titles


def test_export_then_import_restores_secrets(tmp_path: Path) -> None:
    src = _make_home(tmp_path, "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    ensure_schema(dst / "pilk.db")

    bundle = tmp_path / "bundle.zip"
    build_bundle(home=src, output_path=bundle)
    apply_bundle(bundle_bytes=bundle.read_bytes(), target_home=dst)

    dst_secrets = IntegrationSecretsStore(dst / "pilk.db")
    assert dst_secrets.get_value("ghl_api_key") == "pit-test-12345"
    assert dst_secrets.get_value("hunter_io_api_key") == "hunt-test-12345"


def test_export_then_import_restores_accounts(tmp_path: Path) -> None:
    src = _make_home(tmp_path, "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    ensure_schema(dst / "pilk.db")

    bundle = tmp_path / "bundle.zip"
    build_bundle(home=src, output_path=bundle)
    apply_bundle(bundle_bytes=bundle.read_bytes(), target_home=dst)

    # The cloud target should see the operator's Google account after
    # importing the secrets directory.
    dst_accounts = AccountsStore(dst)
    default = dst_accounts.default("google", "user")
    assert default is not None
    assert default.email == "operator@example.com"
    tokens = dst_accounts.load_tokens(default.account_id)
    assert tokens is not None
    assert tokens.access_token == "at-test"
    assert tokens.refresh_token == "rt-test"


def test_import_backs_up_pre_existing_files(tmp_path: Path) -> None:
    src = _make_home(tmp_path, "src")
    dst = _make_home(tmp_path, "dst")  # pre-populated target
    bundle = tmp_path / "bundle.zip"
    build_bundle(home=src, output_path=bundle)

    # Drop a distinctive memory row into the target so we can detect
    # the backup preserved the pre-migration state.
    _insert_memory(dst / "pilk.db", "about-to-be-overwritten")

    report = apply_bundle(
        bundle_bytes=bundle.read_bytes(), target_home=dst
    )
    assert report.ok
    backup_root = Path(report.backup_path)
    assert backup_root.is_dir()
    # The backup must contain the pre-migration pilk.db.
    backed_up_db = backup_root / "pilk.db"
    assert backed_up_db.is_file()

    # Pre-migration row must live on in the backup copy.
    conn = sqlite3.connect(backed_up_db)
    try:
        rows = conn.execute(
            "SELECT title FROM memory_entries"
        ).fetchall()
    finally:
        conn.close()
    titles = {r[0] for r in rows}
    assert "about-to-be-overwritten" in titles


def test_import_refuses_corrupt_zip(tmp_path: Path) -> None:
    report = apply_bundle(
        bundle_bytes=b"not a real zip",
        target_home=tmp_path,
    )
    assert not report.ok
    assert report.error is not None
    assert "zip" in report.error.lower()


def test_import_refuses_missing_manifest(tmp_path: Path) -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pilk.db", b"whatever")

    report = apply_bundle(
        bundle_bytes=buf.getvalue(), target_home=tmp_path
    )
    assert not report.ok
    assert "manifest" in (report.error or "").lower()


def test_import_refuses_mutated_bytes(tmp_path: Path) -> None:
    """Tamper with a file inside the zip after export — importer must
    catch the SHA-256 mismatch and refuse to write anything."""
    import io
    import zipfile

    src = _make_home(tmp_path, "src")
    bundle = tmp_path / "bundle.zip"
    build_bundle(home=src, output_path=bundle)

    # Rewrite pilk.db with different bytes while keeping the manifest
    # (which still has the old hash).
    original = bundle.read_bytes()
    buf_in = io.BytesIO(original)
    buf_out = io.BytesIO()
    with zipfile.ZipFile(buf_in) as zin, zipfile.ZipFile(
        buf_out, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for info in zin.infolist():
            data = (
                b"\x00" * 16 if info.filename == "pilk.db" else zin.read(info.filename)
            )
            zout.writestr(info, data)

    dst = tmp_path / "dst"
    dst.mkdir()
    report = apply_bundle(
        bundle_bytes=buf_out.getvalue(), target_home=dst
    )
    assert not report.ok
    assert "mismatch" in (report.error or "").lower()


def test_import_refuses_oversize_bundle(tmp_path: Path) -> None:
    from core.migration.importer import MAX_BUNDLE_SIZE_BYTES

    huge = b"x" * (MAX_BUNDLE_SIZE_BYTES + 1)
    report = apply_bundle(bundle_bytes=huge, target_home=tmp_path)
    assert not report.ok
    assert "too large" in (report.error or "").lower()


def test_import_report_shape(tmp_path: Path) -> None:
    src = _make_home(tmp_path, "src")
    dst = tmp_path / "dst"
    dst.mkdir()
    ensure_schema(dst / "pilk.db")
    bundle = tmp_path / "bundle.zip"
    build_bundle(home=src, output_path=bundle)

    report = apply_bundle(
        bundle_bytes=bundle.read_bytes(), target_home=dst
    )
    assert isinstance(report, ImportReport)
    assert report.ok
    # Manifest is carried back so the UI can render it verbatim.
    assert report.manifest["bundle_version"] == BUNDLE_VERSION
    assert "pilk.db" in {
        f["path"] for f in report.manifest["files"]
    }
    assert report.files_written >= 1
    assert report.bytes_written > 0
    assert report.backup_path


def test_old_bundle_version_refused(tmp_path: Path) -> None:
    """Manifest with an incompatible bundle_version must be rejected
    before any file writes."""
    import io
    import zipfile

    from core.migration.models import Manifest as ManifestModel

    fake_manifest = ManifestModel(
        bundle_version=BUNDLE_VERSION + 999,  # future version we don't know
        source_schema_version=8,
        created_at=datetime.now(UTC).isoformat() + "Z",
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", fake_manifest.model_dump_json())

    report = apply_bundle(bundle_bytes=buf.getvalue(), target_home=tmp_path)
    assert not report.ok
    assert "bundle_version" in (report.error or "")


def test_export_skips_underscore_clients(tmp_path: Path) -> None:
    src = _make_home(tmp_path, "src")
    clients = tmp_path / "clients"
    clients.mkdir()
    (clients / "_example.yaml").write_text("name: x\n")
    (clients / "acme.yaml").write_text("name: Acme\n")

    bundle = tmp_path / "bundle.zip"
    manifest = build_bundle(
        home=src, output_path=bundle, clients_dir=clients
    )
    archived = {f.path for f in manifest.files}
    assert "clients/acme.yaml" in archived
    assert "clients/_example.yaml" not in archived
    assert manifest.client_count == 1


def test_manifest_json_is_human_readable(tmp_path: Path) -> None:
    """Manifest should be easy to eyeball before upload — operator
    should be able to `unzip -p bundle.zip manifest.json | jq .` and
    know what's inside."""
    import zipfile

    src = _make_home(tmp_path, "src")
    bundle = tmp_path / "bundle.zip"
    build_bundle(home=src, output_path=bundle)

    with zipfile.ZipFile(bundle) as zf:
        raw = zf.read("manifest.json").decode("utf-8")

    payload = json.loads(raw)
    assert payload["bundle_version"] == BUNDLE_VERSION
    assert payload["source_schema_version"] >= 1
    assert "files" in payload
    assert len(payload["files"]) >= 1
