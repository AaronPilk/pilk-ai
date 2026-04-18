from pathlib import Path

import pytest

from core.config import get_settings
from core.db import ensure_schema
from core.registry import AgentRegistry


@pytest.mark.asyncio
async def test_registry_discovers_shipped_agents(tmp_path: Path) -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    repo_root = Path(__file__).resolve().parents[1]
    reg = AgentRegistry(
        manifests_dir=repo_root / "agents", db_path=settings.db_path
    )
    installed = await reg.discover_and_install()
    assert "file_organization_agent" in installed
    # Underscored folders are ignored.
    assert "_template" not in installed


@pytest.mark.asyncio
async def test_registry_ignores_mismatched_folder(tmp_path: Path) -> None:
    settings = get_settings()
    ensure_schema(settings.db_path)
    # Folder name differs from manifest name → skipped
    bad = tmp_path / "not_the_name"
    bad.mkdir()
    (bad / "manifest.yaml").write_text(
        "name: different_name\nversion: 0.1.0\nsystem_prompt: hi\n"
        "tools: [fs_read]\nsandbox:\n  type: process\n  profile: x\n"
    )
    reg = AgentRegistry(manifests_dir=tmp_path, db_path=settings.db_path)
    installed = await reg.discover_and_install()
    assert installed == []
