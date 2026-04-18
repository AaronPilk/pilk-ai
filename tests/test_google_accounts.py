"""Role-aware credential paths + legacy migration."""

from __future__ import annotations

import json
from pathlib import Path

from core.integrations.google import (
    ROLES,
    google_credentials_path,
    google_legacy_path,
    migrate_legacy_if_needed,
)


def test_credentials_paths_and_roles(tmp_path: Path) -> None:
    for role in ROLES:
        p = google_credentials_path(tmp_path, role)
        assert p == tmp_path / "identity" / "integrations" / "google" / f"{role}.json"
    assert google_legacy_path(tmp_path) == (
        tmp_path / "identity" / "integrations" / "google.json"
    )


def test_migrate_legacy_to_system(tmp_path: Path) -> None:
    legacy = google_legacy_path(tmp_path)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"refresh_token": "abc", "email": "x@y"}))

    moved = migrate_legacy_if_needed(tmp_path)
    assert moved is not None
    assert moved == google_credentials_path(tmp_path, "system")
    assert moved.exists()
    assert not legacy.exists()
    assert json.loads(moved.read_text())["refresh_token"] == "abc"


def test_migrate_is_noop_when_no_legacy(tmp_path: Path) -> None:
    assert migrate_legacy_if_needed(tmp_path) is None


def test_migrate_does_not_clobber_existing_system(tmp_path: Path) -> None:
    legacy = google_legacy_path(tmp_path)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"refresh_token": "from_legacy"}))

    existing = google_credentials_path(tmp_path, "system")
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(json.dumps({"refresh_token": "already_linked"}))

    assert migrate_legacy_if_needed(tmp_path) is None
    # Both files still there; neither content was touched.
    assert legacy.exists()
    assert json.loads(legacy.read_text())["refresh_token"] == "from_legacy"
    assert json.loads(existing.read_text())["refresh_token"] == "already_linked"
