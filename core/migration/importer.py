"""Apply a migration bundle to a target pilkd home.

This is the dangerous half — it **overwrites** the target's SQLite
database + OAuth token files. The defensive posture is:

1. **Manifest-first validation.** Parse + schema-check the manifest
   before opening any file. Bad bundle → clean error, nothing touched.

2. **Hash every file.** Each ``FileEntry`` carries a SHA-256; if the
   bundle has been tampered or truncated in transit, we refuse.

3. **Backup before overwrite.** The existing home is snapshotted to
   ``<home>/backups/pre-migration-<timestamp>/`` with every file that
   would be replaced. Rollback is "swap the backup back in."

4. **Atomic per-file apply.** Write each incoming file to
   ``<target>.migrating`` then ``os.replace`` into place so a crash
   mid-apply doesn't leave half-written state.

5. **Report.** Return a structured :class:`ImportReport` with row
   counts, backup path, and any warnings. The caller decides what to
   show the operator.

Scope note: this function writes to disk but **does not restart
pilkd**. Active stores (SQLite connections, in-memory AccountsStore
snapshots, Sentinel supervisor) hold references to the pre-migration
files and won't reflect the new data until a process restart. The
calling route returns an instruction telling the operator to redeploy.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.db.migrations import ensure_schema
from core.migration.models import BUNDLE_VERSION, Manifest


class BundleImportError(Exception):
    """Raised for non-recoverable import failures."""


@dataclass
class ImportReport:
    """Structured outcome of an import attempt. Serialized into the
    HTTP response; the dashboard renders it verbatim."""

    ok: bool
    files_written: int = 0
    bytes_written: int = 0
    backup_path: str = ""
    manifest: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


MAX_BUNDLE_SIZE_BYTES: int = 100 * 1024 * 1024  # 100 MB — generous for SQLite + tokens.


def apply_bundle(
    *,
    bundle_bytes: bytes,
    target_home: Path,
    target_clients_dir: Path | None = None,
) -> ImportReport:
    """Read ``bundle_bytes`` (a zip file in memory) and apply to
    ``target_home``. Always returns an :class:`ImportReport` — never
    raises to the caller unless bytes themselves are unreadable as
    a zip.

    Rollback semantics: on any failure after the backup step, we
    leave the backup in place and return ``ok=False`` with a helpful
    error. The operator can restore manually by copying files back.
    """
    target_home = target_home.expanduser().resolve()
    report = ImportReport(ok=False)

    if len(bundle_bytes) > MAX_BUNDLE_SIZE_BYTES:
        report.error = (
            f"bundle too large: {len(bundle_bytes):,} bytes > "
            f"{MAX_BUNDLE_SIZE_BYTES:,} limit"
        )
        return report

    # Open zip (in-memory) and parse manifest before touching the
    # filesystem.
    import io

    buf = io.BytesIO(bundle_bytes)
    try:
        zf = zipfile.ZipFile(buf)
    except zipfile.BadZipFile as e:
        report.error = f"not a valid zip archive: {e}"
        return report

    try:
        manifest = _read_manifest(zf)
    except BundleImportError as e:
        report.error = str(e)
        return report

    report.manifest = manifest.model_dump()

    if manifest.bundle_version != BUNDLE_VERSION:
        report.error = (
            f"bundle_version={manifest.bundle_version} but importer "
            f"expects {BUNDLE_VERSION}. Upgrade the exporter to match."
        )
        return report

    # Verify every file entry's checksum + presence before writing
    # anything. "Fail fast" — corrupt bundles never touch disk.
    try:
        _verify_bundle(zf, manifest)
    except BundleImportError as e:
        report.error = str(e)
        return report

    # Backup what's currently on disk at the target paths (the files
    # we're about to overwrite, and only those).
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_root = target_home / "backups" / f"pre-migration-{timestamp}"
    try:
        # Flush any pending WAL rows into the main pilk.db file before
        # we copy it; otherwise the backup would miss recent writes.
        existing_db = target_home / "pilk.db"
        if existing_db.is_file():
            _checkpoint_wal(existing_db)

        backup_root.mkdir(parents=True, exist_ok=True)
        for entry in manifest.files:
            target = _target_path_for(entry, target_home, target_clients_dir)
            if target is None:
                continue
            if target.is_file():
                rel = target.relative_to(target_home) if target.is_relative_to(
                    target_home
                ) else Path(target.name)
                dest = backup_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, dest)
    except OSError as e:
        report.error = f"could not create backup at {backup_root}: {e}"
        return report

    report.backup_path = str(backup_root)

    # Apply file-by-file. Each write is:
    #   1. stream to .migrating path
    #   2. os.replace atomically into place
    # Writes to parent directories create them as needed.
    bytes_written = 0
    files_written = 0
    for entry in manifest.files:
        target = _target_path_for(entry, target_home, target_clients_dir)
        if target is None:
            report.warnings.append(
                f"skipped {entry.path}: no mapping for kind={entry.kind}"
            )
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".migrating")
            with zf.open(entry.path, "r") as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(tmp, target)
            bytes_written += entry.size_bytes
            files_written += 1
        except (OSError, KeyError) as e:
            report.error = (
                f"failed to write {entry.path} → {target}: {e}. "
                f"Backup at {backup_root} preserved."
            )
            return report

    # Upgrade the imported SQLite to the current schema. If the bundle
    # was exported from an older version, this runs any newer
    # migrations on top without data loss.
    try:
        ensure_schema(target_home / "pilk.db")
    except sqlite3.Error as e:
        report.warnings.append(
            f"schema upgrade on imported DB raised {type(e).__name__}: {e}"
        )

    report.files_written = files_written
    report.bytes_written = bytes_written
    report.ok = True
    return report


def _checkpoint_wal(db_path: Path) -> None:
    """Flush WAL into the main db file so a plain file copy captures
    every committed row. Soft-fail on any sqlite error."""
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _read_manifest(zf: zipfile.ZipFile) -> Manifest:
    """Parse the manifest.json at the zip root into a validated
    :class:`Manifest`."""
    try:
        raw = zf.read("manifest.json")
    except KeyError as e:
        raise BundleImportError("bundle is missing manifest.json") from e
    try:
        return Manifest.model_validate_json(raw)
    except Exception as e:
        raise BundleImportError(f"invalid manifest.json: {e}") from e


def _verify_bundle(zf: zipfile.ZipFile, manifest: Manifest) -> None:
    """Check that every manifest-listed file is present in the zip
    and its SHA-256 matches the manifest. Refuses the whole import on
    any mismatch."""
    archive_names = set(zf.namelist())
    for entry in manifest.files:
        if entry.path not in archive_names:
            raise BundleImportError(
                f"manifest lists {entry.path!r} but it's missing from the zip"
            )
        hasher = hashlib.sha256()
        with zf.open(entry.path, "r") as f:
            while True:
                block = f.read(64 * 1024)
                if not block:
                    break
                hasher.update(block)
        actual = hasher.hexdigest()
        if actual != entry.sha256:
            raise BundleImportError(
                f"SHA-256 mismatch for {entry.path}: "
                f"manifest says {entry.sha256}, archive hashes to {actual}"
            )


def _target_path_for(
    entry,  # FileEntry
    target_home: Path,
    target_clients_dir: Path | None,
) -> Path | None:
    """Map a bundle entry to its on-disk target in the cloud home."""
    if entry.kind == "sqlite":
        return target_home / "pilk.db"
    if entry.kind == "accounts_index":
        return target_home / "identity" / "accounts" / "index.json"
    if entry.kind == "accounts_secret":
        # Preserve the per-account filename exactly.
        filename = Path(entry.path).name
        return target_home / "identity" / "accounts" / "secrets" / filename
    if entry.kind == "clients_yaml":
        if target_clients_dir is None:
            return None
        return target_clients_dir / Path(entry.path).name
    return None


__all__ = [
    "MAX_BUNDLE_SIZE_BYTES",
    "BundleImportError",
    "ImportReport",
    "apply_bundle",
]
