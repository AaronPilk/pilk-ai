"""Export a local pilkd home into a portable bundle.

The exporter is the "read-only" half — it never modifies the home
it's reading from. Output is a single zip file the operator can
inspect before uploading to cloud.

Shape (all paths relative to the zip root):

    manifest.json
    pilk.db
    identity/accounts/index.json        (if present)
    identity/accounts/secrets/*.json    (OAuth token blobs)
    clients/*.yaml                      (per-client configs, non-_prefix)
"""

from __future__ import annotations

import hashlib
import platform
import sqlite3
import zipfile
from pathlib import Path

from core import __version__
from core.migration.models import (
    FileEntry,
    Manifest,
    TableCounts,
)

# Tables we actually migrate. Anything else in the SQLite file is
# carried over automatically (the whole DB file is copied) — this
# list is only for the report + row-count sanity check.
_MIGRATED_TABLES: tuple[str, ...] = (
    "memory_entries",
    "plans",
    "cost_entries",
    "agent_policies",
    "trust_audit",
    "integration_secrets",
    "xauusd_settings",
    "agent_heartbeats",
    "sentinel_incidents",
)


class ExportError(Exception):
    """Raised for any non-recoverable export failure."""


def build_bundle(
    *,
    home: Path,
    output_path: Path,
    clients_dir: Path | None = None,
) -> Manifest:
    """Package ``home`` into a portable migration bundle.

    The caller is responsible for providing:

    * ``home`` — a pilkd data dir containing ``pilk.db`` and optionally
      ``identity/accounts/``. Must exist.
    * ``output_path`` — where to write the zip. Created; overwrites if
      present. Parent directory must exist.
    * ``clients_dir`` — optional path to a ``clients/*.yaml`` directory
      (repo-relative). Files starting with ``_`` are skipped.

    Returns the :class:`Manifest` that was written into the zip. The
    returned object is the source of truth for the import report.
    """
    home = home.expanduser().resolve()
    output_path = output_path.expanduser().resolve()

    db_path = home / "pilk.db"
    if not db_path.is_file():
        raise ExportError(
            f"no SQLite database at {db_path} — is this a real pilkd home?"
        )

    # Flush WAL into the main .db file so the archived copy is
    # self-contained. Without this, a live pilkd process (or even a
    # recently-closed one) may have unwritten rows in pilk.db-wal that
    # wouldn't make it into the zip.
    _checkpoint_wal(db_path)

    table_counts = _snapshot_table_counts(db_path)
    schema_version = _schema_version(db_path)

    # We collect (archive_path, source_path, kind) tuples first so we
    # can compute checksums once per file and write the manifest with
    # complete data.
    to_archive: list[tuple[str, Path, str]] = []
    to_archive.append(("pilk.db", db_path, "sqlite"))

    accounts_root = home / "identity" / "accounts"
    accounts_index = accounts_root / "index.json"
    secrets_dir = accounts_root / "secrets"
    account_count = 0
    if accounts_index.is_file():
        to_archive.append(
            ("identity/accounts/index.json", accounts_index, "accounts_index")
        )
    if secrets_dir.is_dir():
        for secret in sorted(secrets_dir.glob("*.json")):
            to_archive.append(
                (
                    f"identity/accounts/secrets/{secret.name}",
                    secret,
                    "accounts_secret",
                )
            )
            account_count += 1

    client_count = 0
    if clients_dir is not None and clients_dir.is_dir():
        for yaml_file in sorted(clients_dir.glob("*.yaml")):
            if yaml_file.name.startswith("_"):
                continue
            to_archive.append(
                (f"clients/{yaml_file.name}", yaml_file, "clients_yaml")
            )
            client_count += 1

    files: list[FileEntry] = []
    for archive_path, source_path, kind in to_archive:
        sha, size = _hash_file(source_path)
        files.append(
            FileEntry(
                path=archive_path,
                sha256=sha,
                size_bytes=size,
                kind=kind,  # type: ignore[arg-type]
            )
        )

    manifest = Manifest(
        source_schema_version=schema_version,
        created_at=Manifest.utcnow_iso(),
        source_hostname=platform.node() or "",
        source_home_path=str(home),
        source_pilk_version=__version__,
        files=files,
        table_counts=TableCounts(**{k: v for k, v in table_counts.items()}),
        account_count=account_count,
        client_count=client_count,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as zf:
        zf.writestr("manifest.json", manifest.model_dump_json(indent=2))
        for archive_path, source_path, _ in to_archive:
            zf.write(source_path, arcname=archive_path)

    return manifest


def _checkpoint_wal(db_path: Path) -> None:
    """Force a WAL checkpoint so every committed row lives in the
    main db file. Safe no-op if the DB is in rollback-journal mode."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    except sqlite3.Error:
        # Treat as soft-fail — the hash+bundle step will still
        # succeed; the worst case is stale data, which the operator
        # can fix by re-exporting after stopping pilkd.
        pass
    finally:
        conn.close()


def _snapshot_table_counts(db_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    conn = sqlite3.connect(db_path)
    try:
        for table in _MIGRATED_TABLES:
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()
            except sqlite3.Error:
                # Table might not exist on older homes; treat as zero.
                counts[table] = 0
                continue
            counts[table] = int(row[0]) if row else 0
    finally:
        conn.close()
    return counts


def _schema_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        try:
            row = conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()
        except sqlite3.Error:
            return 0
    finally:
        conn.close()
    return int(row[0]) if row and row[0] is not None else 0


def _hash_file(path: Path, *, chunk: int = 64 * 1024) -> tuple[str, int]:
    """Return (sha256_hex, byte_size) for ``path``."""
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            hasher.update(block)
            size += len(block)
    return hasher.hexdigest(), size


__all__ = ["ExportError", "build_bundle"]
