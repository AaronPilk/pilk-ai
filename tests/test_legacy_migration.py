"""Batch K → AccountsStore one-shot migration."""

from __future__ import annotations

import json
from pathlib import Path

from core.identity import AccountsStore
from core.integrations.legacy_migration import migrate_batch_k_google_files


def _write_legacy(home: Path, role: str, email: str) -> Path:
    path = home / "identity" / "integrations" / "google" / f"{role}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "refresh_token": f"rt_{role}",
                "access_token": f"at_{role}",
                "client_id": "cid",
                "client_secret": "csec",
                "scopes": ["https://mail/send"],
                "email": email,
            },
        ),
    )
    return path


def test_migration_imports_system_and_user(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    sys_file = _write_legacy(tmp_path, "system", "sentientpilkai@gmail.com")
    usr_file = _write_legacy(tmp_path, "user", "aaron@work.com")

    imported = migrate_batch_k_google_files(tmp_path, store)
    assert len(imported) == 2

    # Originals renamed (not deleted) so the user can verify.
    assert not sys_file.exists()
    assert sys_file.with_suffix(".json.migrated").exists()
    assert not usr_file.exists()
    assert usr_file.with_suffix(".json.migrated").exists()

    # Both roles are linked and each became default for its slot.
    sys_default = store.default("google", "system")
    usr_default = store.default("google", "user")
    assert sys_default is not None
    assert sys_default.email == "sentientpilkai@gmail.com"
    assert usr_default is not None
    assert usr_default.email == "aaron@work.com"


def test_migration_is_idempotent(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    _write_legacy(tmp_path, "system", "sentientpilkai@gmail.com")

    first = migrate_batch_k_google_files(tmp_path, store)
    second = migrate_batch_k_google_files(tmp_path, store)
    assert len(first) == 1
    assert second == []  # no-op on second run
    assert len(store.list(provider="google", role="system")) == 1


def test_migration_noop_without_legacy_files(tmp_path: Path) -> None:
    store = AccountsStore(tmp_path)
    store.ensure_layout()
    assert migrate_batch_k_google_files(tmp_path, store) == []
    assert store.list() == []
