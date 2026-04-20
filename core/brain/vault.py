"""Vault — PILK's Obsidian-compatible long-term brain.

Thin filesystem wrapper around a directory of markdown files. The
operator can open the same directory in Obsidian desktop for a
graph / backlink view; PILK reads + writes plain .md files through
this module.

Design notes:

- The vault root is configurable via ``PILK_BRAIN_VAULT_PATH``
  (defaults to ``~/PILK-brain``). Created on boot if missing, with a
  README.md that orients the operator and seeds the graph.
- Every path is normalised to a ``.md`` extension and checked to
  stay inside the vault root — no absolute paths, no ``../`` escape.
- Reads are UTF-8 text; binary/garbled bytes are replaced rather
  than raising. Writes are atomic (write to tempfile, then rename).
- Search is plain case-insensitive substring. Sufficient for tens
  of thousands of notes; a more capable index can land later.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_READ_BYTES = 512 * 1024       # 512 KiB — generous for a note
MAX_WRITE_BYTES = 1 * 1024 * 1024  # 1 MiB per write
MAX_SEARCH_HITS = 50
SEARCH_SNIPPET_CHARS = 160

_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9 _\-./()']+$")


@dataclass(frozen=True)
class SearchHit:
    path: str           # vault-relative, POSIX style ("folder/note.md")
    line: int           # 1-indexed
    snippet: str        # up to SEARCH_SNIPPET_CHARS around the match


class Vault:
    """Filesystem-backed Obsidian-compatible vault."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    def ensure_initialized(self) -> None:
        """Create the vault directory + starter README if missing. Safe
        to call on every boot — we only write the README when there
        are zero markdown files in the root, so an in-use vault is
        never disturbed."""
        self._root.mkdir(parents=True, exist_ok=True)
        has_md = any(self._root.rglob("*.md"))
        if not has_md:
            starter = self._root / "README.md"
            starter.write_text(_STARTER_README, encoding="utf-8")

    def resolve(self, rel: str) -> Path:
        """Turn a user-supplied vault-relative path into an absolute
        path, rejecting anything that tries to escape the vault."""
        raw = (rel or "").strip()
        if not raw:
            raise ValueError("path is required")
        # Disallow leading slash / absolute paths; the vault's own root
        # is implicit. Disallow `..` segments anywhere.
        if raw.startswith("/") or raw.startswith("~"):
            raise ValueError("path must be relative to the vault root")
        if not raw.endswith(".md"):
            raw = raw + ".md"
        # Normalise separators + reject suspicious chars. Markdown
        # filenames from an LLM should be plain; reject anything that
        # includes shell metacharacters, null bytes, etc.
        if not _SAFE_FILENAME_RE.match(raw):
            raise ValueError(
                "path contains disallowed characters; use letters, "
                "numbers, spaces, dashes, underscores, dots, and slashes"
            )
        candidate = (self._root / raw).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as e:
            raise ValueError(f"path escapes vault: {rel}") from e
        return candidate

    def read(self, rel: str) -> str:
        path = self.resolve(rel)
        if not path.exists():
            raise FileNotFoundError(f"not found: {rel}")
        if not path.is_file():
            raise IsADirectoryError(f"not a file: {rel}")
        raw = path.read_bytes()
        truncated = len(raw) > MAX_READ_BYTES
        body = raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        if truncated:
            body += f"\n\n[truncated — {len(raw)} bytes, shown {MAX_READ_BYTES}]"
        return body

    def write(self, rel: str, content: str, *, append: bool = False) -> Path:
        path = self.resolve(rel)
        data = (content or "").encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ValueError(
                f"content exceeds per-write cap "
                f"({len(data)} > {MAX_WRITE_BYTES} bytes)"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        if append and path.exists():
            existing = path.read_bytes()
            new = existing + b"\n\n" + data
            if len(new) > MAX_WRITE_BYTES:
                raise ValueError(
                    f"append would exceed per-write cap "
                    f"({len(new)} > {MAX_WRITE_BYTES} bytes)"
                )
            data = new
        # Atomic write: tempfile on the same directory → rename.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".pilk-", suffix=".md.tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_name, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        return path

    def list(self, folder: str | None = None) -> list[str]:
        """Return sorted vault-relative POSIX paths of every .md file
        under ``folder`` (or the whole vault if None)."""
        base = self._root
        if folder:
            base = self.resolve(folder).parent if folder.endswith(".md") else (
                self._root / folder.strip("/")
            ).resolve()
            try:
                base.relative_to(self._root)
            except ValueError as e:
                raise ValueError(f"folder escapes vault: {folder}") from e
            if not base.exists():
                return []
        out: list[str] = []
        for md in base.rglob("*.md"):
            rel = md.relative_to(self._root).as_posix()
            out.append(rel)
        out.sort()
        return out

    def search(self, query: str, *, limit: int = MAX_SEARCH_HITS) -> list[SearchHit]:
        """Case-insensitive substring search across every .md file.
        Returns up to ``limit`` hits with a short snippet per hit."""
        q = (query or "").strip()
        if not q:
            return []
        needle = q.lower()
        hits: list[SearchHit] = []
        for md in self._root.rglob("*.md"):
            try:
                lines = md.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue
            rel = md.relative_to(self._root).as_posix()
            for idx, line in enumerate(lines, start=1):
                if needle not in line.lower():
                    continue
                snippet = line.strip()
                if len(snippet) > SEARCH_SNIPPET_CHARS:
                    # Center the snippet on the first match.
                    pos = snippet.lower().find(needle)
                    half = SEARCH_SNIPPET_CHARS // 2
                    start = max(0, pos - half)
                    end = min(len(snippet), start + SEARCH_SNIPPET_CHARS)
                    snippet = ("…" if start > 0 else "") + snippet[start:end] + (
                        "…" if end < len(snippet) else ""
                    )
                hits.append(SearchHit(path=rel, line=idx, snippet=snippet))
                if len(hits) >= limit:
                    return hits
        return hits


_STARTER_README = """# PILK brain

This folder is PILK's long-term knowledge store. Everything PILK
learns about your work, clients, systems, and decisions — the things
too long to live as a short `memory_remember` entry — lands here as
a markdown note.

You can open this directory as an Obsidian vault to get graph +
backlink navigation. Files are plain `.md` so you can also just
browse them in Finder or edit them in any text editor.

## Conventions PILK tries to follow
- One topic per note.
- Title is the filename stem (so `Aaron's trading rules.md` → "Aaron's trading rules").
- Wikilinks in `[[Note Title]]` form to connect related notes.
- Front-matter is optional.

PILK will start populating this vault whenever it encounters something
worth writing down at length. Short facts still go into the structured
memory on the Memory page.
"""
