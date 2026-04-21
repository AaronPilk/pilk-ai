"""Tests for the trigger registry.

The registry is the glue between ``triggers/*/manifest.yaml`` on disk
and the ``triggers`` table in SQLite. These tests exercise:
- discover_and_install upserts every valid manifest
- folder/name mismatches are skipped with a log
- enabled-state in SQLite wins over a changed manifest default
- last_fired_at round-trips through mark_fired
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from core.config import get_settings
from core.db import ensure_schema
from core.triggers import TriggerNotFoundError, TriggerRegistry


def _manifest(dir_: Path, name: str, body: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "manifest.yaml").write_text(dedent(body), encoding="utf-8")


def _basic(name: str = "nightly_summary", enabled: bool = True) -> str:
    return f"""
        name: {name}
        agent_name: inbox_triage_agent
        goal: "Do the thing."
        enabled: {str(enabled).lower()}
        schedule:
          kind: cron
          expression: "0 22 * * *"
        """


# ── happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_installs_every_valid_manifest(tmp_path: Path) -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    _manifest(tmp_path / "nightly_summary", "nightly_summary", _basic())
    _manifest(tmp_path / "morning_digest",
              "morning_digest", _basic(name="morning_digest", enabled=False))
    # Underscored folder → skipped.
    _manifest(tmp_path / "_template", "_template", _basic(name="_template"))
    reg = TriggerRegistry(
        manifests_dir=tmp_path, db_path=settings.db_path,
    )
    installed = await reg.discover_and_install()
    assert set(installed) == {"nightly_summary", "morning_digest"}
    assert reg.enabled("nightly_summary") is True
    assert reg.enabled("morning_digest") is False


@pytest.mark.asyncio
async def test_mismatched_folder_skipped(tmp_path: Path) -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    _manifest(tmp_path / "wrong_folder", "real_name", _basic(name="real_name"))
    reg = TriggerRegistry(
        manifests_dir=tmp_path, db_path=settings.db_path,
    )
    installed = await reg.discover_and_install()
    assert installed == []


@pytest.mark.asyncio
async def test_invalid_manifest_skipped_not_fatal(tmp_path: Path) -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    _manifest(tmp_path / "bad", "bad", "name: bad\nnot_valid: yes\n")
    _manifest(tmp_path / "good", "good", _basic(name="good"))
    reg = TriggerRegistry(
        manifests_dir=tmp_path, db_path=settings.db_path,
    )
    installed = await reg.discover_and_install()
    assert installed == ["good"]


# ── enabled-state persistence ───────────────────────────────────


@pytest.mark.asyncio
async def test_db_enabled_state_wins_over_manifest_default(
    tmp_path: Path,
) -> None:
    """Operator toggles trigger off via UI → SQLite has enabled=0.
    Next boot, even if the manifest still says enabled: true, the
    SQLite value wins."""
    settings = get_settings()
    ensure_schema(settings.db_path)
    _manifest(tmp_path / "daily", "daily", _basic(name="daily", enabled=True))
    reg = TriggerRegistry(
        manifests_dir=tmp_path, db_path=settings.db_path,
    )
    await reg.discover_and_install()
    assert reg.enabled("daily") is True
    await reg.set_enabled("daily", False)

    # Fresh registry to simulate a restart — reuse the same DB.
    reg2 = TriggerRegistry(
        manifests_dir=tmp_path, db_path=settings.db_path,
    )
    await reg2.discover_and_install()
    assert reg2.enabled("daily") is False


@pytest.mark.asyncio
async def test_set_enabled_on_unknown_trigger_raises(
    tmp_path: Path,
) -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    reg = TriggerRegistry(
        manifests_dir=tmp_path, db_path=settings.db_path,
    )
    with pytest.raises(TriggerNotFoundError):
        await reg.set_enabled("nonexistent", True)


# ── mark_fired round-trip ───────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_fired_persists_timestamp(tmp_path: Path) -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    _manifest(tmp_path / "daily", "daily", _basic(name="daily"))
    reg = TriggerRegistry(
        manifests_dir=tmp_path, db_path=settings.db_path,
    )
    await reg.discover_and_install()
    stamp = await reg.mark_fired("daily")
    assert isinstance(stamp, str)
    assert reg.last_fired_at("daily") == stamp

    # Survives a restart.
    reg2 = TriggerRegistry(
        manifests_dir=tmp_path, db_path=settings.db_path,
    )
    await reg2.discover_and_install()
    assert reg2.last_fired_at("daily") == stamp


# ── list_rows shape ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_rows_includes_manifest_fields(tmp_path: Path) -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    _manifest(tmp_path / "daily", "daily", _basic(name="daily"))
    reg = TriggerRegistry(
        manifests_dir=tmp_path, db_path=settings.db_path,
    )
    await reg.discover_and_install()
    rows = await reg.list_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "daily"
    assert row["agent_name"] == "inbox_triage_agent"
    assert row["schedule"]["kind"] == "cron"
    assert row["schedule"]["expression"] == "0 22 * * *"
    assert row["enabled"] is True
    assert row["last_fired_at"] is None
