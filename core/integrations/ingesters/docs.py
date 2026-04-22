"""Docs-folder ingester.

Walks an operator-chosen folder (typically under ``~/Documents``),
reads plain-text files, and stages them as markdown notes in the
brain vault under ``ingested/docs/<original-rel-path>.md``. The
original folder structure is preserved so the Obsidian graph
reflects the source layout.

Scope is deliberately narrow for v1:

* Text-native formats only — ``.md``, ``.txt``, ``.markdown``,
  ``.rtf``, ``.html``, ``.log``, ``.csv``, ``.json``, ``.yaml``,
  ``.yml``, ``.tsv``. PDFs / docx live in a follow-up once
  ``pypdf`` / ``python-docx`` land in deps.
* Files over ``MAX_FILE_BYTES`` are skipped.
* Files that fail utf-8 decode fall back to latin-1; if that also
  blows up the file is skipped with an entry in the error list.

Safety: the *source* path must live inside the operator's home
directory. Ingesters are called by tool handlers that have already
confirmed ``COMPUTER_CONTROL_ENABLED`` — the home-scope clamp here
is a defence-in-depth check against accidentally pointing the
walker at ``/etc`` or ``/var``.

The ingester is pure — it returns a list of ``IngestedNote`` without
writing anything. The tool layer owns the vault writes so this
module stays trivially testable.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.integrations.ingesters import IngestedNote
from core.logging import get_logger

log = get_logger("pilkd.ingest.docs")

# ── limits ──────────────────────────────────────────────────────────

MAX_FILE_BYTES = 5 * 1024 * 1024        # 5 MiB per file
MAX_NOTE_BODY_CHARS = 200_000           # clamp enormous logs in-note
DEFAULT_MAX_FILES = 1_000
HARD_MAX_FILES = 10_000
DEFAULT_EXTENSIONS: tuple[str, ...] = (
    ".md",
    ".markdown",
    ".txt",
    ".rtf",
    ".html",
    ".htm",
    ".log",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
)

# ── value types ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class DocFile:
    """One readable source file the walker surfaced."""

    abs_path: Path
    rel_path: Path        # relative to the scan root (for vault placement)
    size: int
    mtime: datetime
    text: str             # decoded body, already clamped to MAX_NOTE_BODY_CHARS


@dataclass(frozen=True)
class ScanResult:
    """Summary of a single scan pass."""

    root: Path
    found: list[DocFile]
    skipped: list[tuple[Path, str]]   # (path, reason)


class DocsIngestError(ValueError):
    """Raised for operator-visible validation problems (bad root,
    out-of-home path, unreadable root). File-level failures land in
    ``ScanResult.skipped`` instead."""


# ── public surface ──────────────────────────────────────────────────


def scan_docs(
    root: Path,
    *,
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
    max_files: int = DEFAULT_MAX_FILES,
    recursive: bool = True,
    home: Path | None = None,
) -> ScanResult:
    """Walk ``root`` and return every readable text file up to
    ``max_files``. Non-text formats and oversized files land in
    ``skipped`` so the tool layer can report them."""
    root = root.expanduser().resolve()
    cap = min(max(max_files, 1), HARD_MAX_FILES)
    allowed_suffixes = frozenset(e.lower() for e in extensions)

    if not root.exists():
        raise DocsIngestError(f"source_dir not found: {root}")
    if not root.is_dir():
        raise DocsIngestError(f"source_dir is not a directory: {root}")

    home_root = (home or Path.home()).expanduser().resolve()
    if home_root not in root.parents and root != home_root:
        raise DocsIngestError(
            f"source_dir must live under {home_root}; refusing {root}"
        )

    iterator = _walk(root, recursive=recursive)

    found: list[DocFile] = []
    skipped: list[tuple[Path, str]] = []
    for path in iterator:
        if len(found) >= cap:
            skipped.append((path, "max_files cap reached"))
            break
        if path.name.startswith("."):
            skipped.append((path, "hidden"))
            continue
        suffix = path.suffix.lower()
        if suffix not in allowed_suffixes:
            continue  # silent skip; don't spam the summary with every .jpg
        try:
            stat = path.stat()
        except OSError as e:
            skipped.append((path, f"stat failed: {e}"))
            continue
        if stat.st_size > MAX_FILE_BYTES:
            skipped.append((path, f"too large ({stat.st_size} bytes)"))
            continue
        decoded = _read_text(path)
        if decoded is None:
            skipped.append((path, "undecodable"))
            continue
        rel = _safe_relative(path, root)
        found.append(
            DocFile(
                abs_path=path,
                rel_path=rel,
                size=stat.st_size,
                mtime=datetime.fromtimestamp(stat.st_mtime, UTC),
                text=decoded[:MAX_NOTE_BODY_CHARS],
            )
        )
    return ScanResult(root=root, found=found, skipped=skipped)


def render_doc_note(doc: DocFile, *, scan_root: Path) -> IngestedNote:
    """Convert one ``DocFile`` into an ``IngestedNote`` ready for the
    vault. Markdown passes through; other text formats get wrapped in
    a fenced block so Obsidian renders them cleanly."""
    rel = doc.rel_path.as_posix()
    note_path = _note_path_for(rel)
    title = doc.abs_path.stem
    body = _render_body(doc, scan_root=scan_root)
    return IngestedNote(
        path=note_path,
        body=body,
        source_id=str(doc.abs_path),
        title=title,
    )


# ── internals ───────────────────────────────────────────────────────


def _walk(root: Path, *, recursive: bool):
    if recursive:
        yield from (Path(p) for p in _iter_recursive(root))
    else:
        yield from (p for p in root.iterdir() if p.is_file())


def _iter_recursive(root: Path):
    # os.walk is faster than Path.rglob on huge trees and we can
    # prune dotfile directories inline rather than post-filtering.
    for base, dirs, files in os.walk(root, followlinks=False):
        # Mutating `dirs` in place prunes the traversal.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            yield os.path.join(base, name)


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError as e:
        log.warning("docs_ingest_read_failed", path=str(path), error=str(e))
        return None
    for encoding in ("utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _safe_relative(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        # Symlink or path escape — fall back to the basename so the
        # vault path still resolves cleanly.
        return Path(path.name)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(s: str) -> str:
    cleaned = _SLUG_RE.sub("-", s.lower()).strip("-")
    return cleaned or "untitled"


def _note_path_for(rel_posix: str) -> str:
    """Mirror the source structure under ``ingested/docs/`` so the
    Obsidian graph groups by original folder. Each path segment is
    slugged to keep the vault file-system portable, but the original
    name is preserved in the YAML frontmatter."""
    parts = [p for p in rel_posix.split("/") if p]
    if not parts:
        return "ingested/docs/untitled.md"
    segments = [_slug(p) for p in parts[:-1]]
    leaf = Path(parts[-1])
    stem_slug = _slug(leaf.stem) or "untitled"
    segments.append(f"{stem_slug}.md")
    return "ingested/docs/" + "/".join(segments)


def _render_body(doc: DocFile, *, scan_root: Path) -> str:
    frontmatter = (
        "---\n"
        f"source: {doc.abs_path}\n"
        f"source_rel: {doc.rel_path.as_posix()}\n"
        f"scan_root: {scan_root}\n"
        f"ingested_at: {datetime.now(UTC).isoformat()}\n"
        f"size_bytes: {doc.size}\n"
        f"mtime: {doc.mtime.isoformat()}\n"
        "tags: [ingested, docs]\n"
        "---\n\n"
    )
    heading = f"# {doc.abs_path.stem}\n\n"
    suffix = doc.abs_path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        # Markdown lands verbatim so existing wiki-links / headings
        # survive the trip into the vault.
        rendered = doc.text.rstrip() + "\n"
    elif suffix in {".html", ".htm"}:
        rendered = _strip_html(doc.text).rstrip() + "\n"
    else:
        fence_lang = _fence_lang_for(suffix)
        rendered = (
            f"```{fence_lang}\n"
            + doc.text.rstrip()
            + "\n```\n"
        )
    return frontmatter + heading + rendered


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    without_tags = _HTML_TAG_RE.sub("", text)
    return html.unescape(without_tags)


def _fence_lang_for(suffix: str) -> str:
    return {
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".csv": "csv",
        ".tsv": "tsv",
        ".log": "log",
        ".rtf": "rtf",
    }.get(suffix, "text")


__all__ = [
    "DEFAULT_EXTENSIONS",
    "DEFAULT_MAX_FILES",
    "HARD_MAX_FILES",
    "MAX_FILE_BYTES",
    "DocFile",
    "DocsIngestError",
    "ScanResult",
    "render_doc_note",
    "scan_docs",
]
