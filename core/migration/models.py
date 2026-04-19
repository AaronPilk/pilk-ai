"""Bundle manifest for local→cloud migration.

Every migration bundle ships with a ``manifest.json`` at the top of
the zip describing exactly what's inside. The importer validates this
before touching anything on disk — version mismatches, missing files,
and obvious tampering fail cleanly before overwriting cloud state.

The format is conservative on purpose: one bundle version per
breaking change, full checksum of every archived file, explicit
origin + creation timestamp. You can read a bundle manifest by hand
and know exactly what gets written.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Bump when the bundle shape changes in a way the importer can't
# back-compat. Minor table additions don't need a bump — the importer
# migrates the restored DB up to CURRENT_VERSION anyway.
BUNDLE_VERSION: int = 1


class FileEntry(BaseModel):
    """One file inside the bundle + its integrity metadata."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=400)
    # SHA-256 hex; the importer recomputes and refuses on mismatch.
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    # Short description surfaced in the import report.
    kind: Literal[
        "sqlite",
        "accounts_index",
        "accounts_secret",
        "clients_yaml",
        "other",
    ]


class TableCounts(BaseModel):
    """Row counts per SQLite table. Useful for the import report
    without streaming the DB twice."""

    model_config = ConfigDict(extra="allow")

    memory_entries: int = 0
    plans: int = 0
    cost_entries: int = 0
    agent_policies: int = 0
    trust_audit: int = 0
    integration_secrets: int = 0
    xauusd_settings: int = 0
    agent_heartbeats: int = 0
    sentinel_incidents: int = 0


class Manifest(BaseModel):
    """The bundle manifest. Stored as ``manifest.json`` at the zip root."""

    model_config = ConfigDict(extra="forbid")

    bundle_version: int = Field(default=BUNDLE_VERSION)
    # Schema migration version at export time. The importer applies its
    # own schema migrations on top; this is just a sanity field.
    source_schema_version: int
    # ISO-8601 UTC. Bundles older than a few days should warn the
    # operator ("do you really want to overwrite a month of cloud data
    # with this?") — the check lives in the importer.
    created_at: str
    # Freeform: e.g. `"Aarons-MBP"` or `"pilkai-production"`. No
    # validation — purely informative in the report.
    source_hostname: str = Field(default="", max_length=120)
    source_home_path: str = Field(default="", max_length=400)
    # PILK version at export time. A mismatch with the importer's
    # version logs a warning but does not fail.
    source_pilk_version: str = Field(default="", max_length=40)
    # Every file archived, in archive order. The importer iterates
    # this list to verify + lay down each file.
    files: list[FileEntry] = Field(default_factory=list)
    # Populated from the SQLite snapshot at export time.
    table_counts: TableCounts = Field(default_factory=TableCounts)
    # Number of OAuth-token JSON blobs bundled.
    account_count: int = 0
    # Number of clients/*.yaml files bundled.
    client_count: int = 0

    @classmethod
    def utcnow_iso(cls) -> str:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"


__all__ = [
    "BUNDLE_VERSION",
    "FileEntry",
    "Manifest",
    "TableCounts",
]
