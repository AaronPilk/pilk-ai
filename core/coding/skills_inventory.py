"""Inventory of Claude Code skills + plugins installed on the host.

Scans ``~/.claude/skills/`` and ``~/.claude/plugins/`` (or an override
root) and returns a flat list of what's there. Each entry includes the
top-level name, what kind it is, the absolute path, and a short
description pulled from ``SKILL.md``'s first non-empty line when one
exists.

The Claude Code CLI owns these directories — PILK never writes into
them, and the inventory is strictly read-only. The purpose is
visibility: show the operator which ambient capabilities their
PILK → Claude Code bridge calls will inherit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_CLAUDE_HOME = Path("~/.claude").expanduser()
SKILL_SUBDIR = "skills"
PLUGIN_SUBDIR = "plugins"
DESCRIPTION_MAX_CHARS = 200


@dataclass(frozen=True)
class InstalledPack:
    """One installed skill or plugin, as it appears under the Claude
    Code config directory. We don't recurse further than a couple of
    levels — individual SKILL.md files nested deeper are still
    useful, but enumerating them all would be noise."""

    name: str
    kind: str  # "skill" | "plugin"
    path: str
    description: str  # "" when nothing informative was found


def inventory(claude_home: Path | None = None) -> dict[str, list[InstalledPack]]:
    """Return ``{"skills": [...], "plugins": [...]}``.

    Missing directories (common on a fresh Mac or on Railway) yield
    empty lists, not errors. Each list is sorted by name so the UI
    renders predictably.
    """
    home = (claude_home or DEFAULT_CLAUDE_HOME).expanduser()
    return {
        "skills": _scan(home / SKILL_SUBDIR, kind="skill"),
        "plugins": _scan(home / PLUGIN_SUBDIR, kind="plugin"),
    }


def _scan(root: Path, *, kind: str) -> list[InstalledPack]:
    if not root.exists() or not root.is_dir():
        return []
    out: list[InstalledPack] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        # Skip hidden / OS metadata entries.
        if entry.name.startswith("."):
            continue
        # Accept both directories (the common case — a cloned git
        # repo) and single-file skills (rarer).
        if not entry.is_dir() and entry.suffix.lower() not in (".md",):
            continue
        out.append(
            InstalledPack(
                name=entry.name,
                kind=kind,
                path=str(entry),
                description=_describe(entry),
            )
        )
    return out


def _describe(entry: Path) -> str:
    """Pull a human-readable blurb. Priority order:
    1. Top-level SKILL.md / README.md first meaningful line
    2. Any nested SKILL.md's first line (shallow search, one level)
    3. Empty string
    """
    candidates: list[Path] = []
    if entry.is_file() and entry.suffix.lower() == ".md":
        candidates.append(entry)
    elif entry.is_dir():
        for filename in ("SKILL.md", "README.md", "README"):
            p = entry / filename
            if p.is_file():
                candidates.append(p)
        # Shallow nested probe — many skill bundles have
        # <name>/<sub-skill>/SKILL.md but no top-level description.
        if not candidates:
            for sub in entry.iterdir():
                if sub.is_dir() and (sub / "SKILL.md").is_file():
                    candidates.append(sub / "SKILL.md")
                    break

    for md in candidates:
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Prefer the first content line (body text) over the heading —
        # a body sentence is usually more informative than the title.
        # Fall back to the stripped heading when there's no body yet.
        first_heading: str | None = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("---"):  # front-matter opener/closer
                continue
            if line.startswith("#"):
                if first_heading is None:
                    first_heading = line.lstrip("#").strip()
                continue
            return _clamp(line)
        if first_heading:
            return _clamp(first_heading)
    return ""


def _clamp(line: str) -> str:
    if len(line) > DESCRIPTION_MAX_CHARS:
        return line[: DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"
    return line
