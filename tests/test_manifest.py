from pathlib import Path

import pytest
from pydantic import ValidationError

from core.registry.manifest import Manifest

MINIMAL = """
name: demo
version: 0.1.0
description: d
system_prompt: |
  be helpful
tools: [fs_read, fs_write]
sandbox:
  type: process
  profile: demo
"""


def test_minimal_manifest_parses(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(MINIMAL)
    m = Manifest.load(path)
    assert m.name == "demo"
    assert m.tools == ["fs_read", "fs_write"]
    assert m.sandbox.type == "process"
    assert m.policy.budget.per_run_usd > 0


def test_invalid_agent_name_rejected(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(
        MINIMAL.replace("name: demo", "name: Bad-Name!")
    )
    with pytest.raises(ValidationError):
        Manifest.load(path)


def test_shipped_agents_parse() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    agents_dir = repo_root / "agents"
    parsed = 0
    for sub in agents_dir.iterdir():
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        manifest = sub / "manifest.yaml"
        if manifest.exists():
            Manifest.load(manifest)
            parsed += 1
    assert parsed >= 1, "no first-party agents shipped"
