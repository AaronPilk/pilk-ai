"""Tests for the Claude Code skills + plugins inventory scanner.

All tests run against a tmp_path fake Claude home — no touching the
real ~/.claude.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.routes.coding import router as coding_router
from core.coding.skills_inventory import inventory


def test_inventory_missing_home(tmp_path: Path) -> None:
    """No ~/.claude at all → empty lists, not an error."""
    result = inventory(tmp_path / "does-not-exist")
    assert result == {"skills": [], "plugins": []}


def test_inventory_empty_dirs(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / "plugins").mkdir()
    result = inventory(tmp_path)
    assert result == {"skills": [], "plugins": []}


def test_inventory_skill_with_top_level_skill_md(tmp_path: Path) -> None:
    """Body line wins over heading — it's more informative than the
    title alone."""
    sk = tmp_path / "skills" / "superpowers"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "# Superpowers\n\nAgentic skills framework for software dev.\n"
    )
    result = inventory(tmp_path)
    assert len(result["skills"]) == 1
    entry = result["skills"][0]
    assert entry.name == "superpowers"
    assert entry.kind == "skill"
    assert "Agentic skills framework" in entry.description
    assert str(sk) in entry.path


def test_inventory_skill_falls_back_to_readme(tmp_path: Path) -> None:
    sk = tmp_path / "skills" / "claude-ads"
    sk.mkdir(parents=True)
    (sk / "README.md").write_text(
        "# Claude Ads\n\nComprehensive paid-ads audit skill.\n"
    )
    result = inventory(tmp_path)
    assert "paid-ads audit" in result["skills"][0].description


def test_inventory_heading_only_file(tmp_path: Path) -> None:
    """When the README is just a heading with no body, fall back to
    the stripped heading so the description isn't empty."""
    sk = tmp_path / "skills" / "bare"
    sk.mkdir(parents=True)
    (sk / "README.md").write_text("# Just a title\n")
    result = inventory(tmp_path)
    assert result["skills"][0].description == "Just a title"


def test_inventory_skill_nested_probe(tmp_path: Path) -> None:
    """Bundles like `everything-claude-code` nest their SKILL.md files
    one level down. The probe should pick up the first one it sees."""
    nested = tmp_path / "skills" / "everything" / "pdf-writer"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("# PDF Writer\n\nPDFs from prompts.\n")
    result = inventory(tmp_path)
    entry = result["skills"][0]
    assert entry.name == "everything"
    assert "PDFs from prompts" in entry.description


def test_inventory_plugin(tmp_path: Path) -> None:
    plug = tmp_path / "plugins" / "claude-mem"
    plug.mkdir(parents=True)
    (plug / "README.md").write_text(
        "# claude-mem\n\nCross-session memory plugin for Claude Code.\n"
    )
    result = inventory(tmp_path)
    assert len(result["plugins"]) == 1
    entry = result["plugins"][0]
    assert entry.kind == "plugin"
    assert entry.name == "claude-mem"
    assert "cross-session" in entry.description.lower()


def test_inventory_skips_hidden_entries(tmp_path: Path) -> None:
    (tmp_path / "skills" / ".DS_Store").mkdir(parents=True)
    (tmp_path / "skills" / ".git").mkdir(parents=True)
    result = inventory(tmp_path)
    assert result["skills"] == []


def test_inventory_sorts_alphabetically(tmp_path: Path) -> None:
    for name in ["zzz", "aaa", "mmm"]:
        (tmp_path / "skills" / name).mkdir(parents=True)
    result = inventory(tmp_path)
    assert [p.name for p in result["skills"]] == ["aaa", "mmm", "zzz"]


def test_route_returns_json_shape(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "skills" / "superpowers").mkdir(parents=True)
    (tmp_path / "plugins" / "claude-mem").mkdir(parents=True)
    # Override the default home the route scans.
    import core.api.routes.coding as coding_route

    def _stub_inventory(*_args, **_kwargs):
        from core.coding.skills_inventory import inventory as real_inv

        return real_inv(tmp_path)

    monkeypatch.setattr(coding_route, "inventory_skills", _stub_inventory)
    app = FastAPI()
    app.include_router(coding_router)
    r = TestClient(app).get("/coding/skills")
    assert r.status_code == 200
    body = r.json()
    assert {"skills", "plugins"} == set(body.keys())
    assert body["skills"][0]["name"] == "superpowers"
    assert body["plugins"][0]["name"] == "claude-mem"
