"""HTTP surface for the long-term Obsidian brain vault.

  GET    /brain                       list every note in the vault
  GET    /brain/note?path=folder/x    read one note (markdown body)
  GET    /brain/search?q=…            substring search across notes
  GET    /brain/graph                 {nodes, edges} from every note's
                                      wiki-links — feeds the dashboard
                                      force-directed graph view.
  GET    /brain/backlinks?path=…      which other notes link to this one
  POST   /brain/upload                multipart PDF/.txt → new note
  PATCH  /brain/note                  {path, content} overwrite body
  DELETE /brain/note?path=…           remove a note from the vault

The vault is a plain folder of markdown files under
``PILK_BRAIN_VAULT_PATH`` (default ``~/PILK-brain``). Opening the same
folder in Obsidian gives graph + backlink navigation on exactly the
same files the dashboard reads through here. Writes from the agent
loop still go through `brain_note_write` — these HTTP write endpoints
share the same Vault object and therefore the same safety checks
(path escape guards, size caps, atomic writes).

We keep this route thin on purpose: pagination and fancy tree
construction live on the client. The server returns:

- list → {notes: [{path, folder, stem, size, mtime}], root}
- read → {path, body, size}
- search → {query, hits: [{path, line, snippet}]}
- graph → {nodes: [{id, label, folder, size}], edges: [{source, target}]}
- backlinks → {target, links: [{path, line, snippet}]}

so the UI can render its own tree without a second round-trip.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from core.brain import Vault
from core.logging import get_logger

log = get_logger("pilkd.brain.route")

router = APIRouter(prefix="/brain")

# Don't let the UI torch the server with pathological search terms. The
# vault itself caps at 50 hits per search already; this is a client-
# input sanity bound.
SEARCH_MIN_CHARS = 2
SEARCH_MAX_CHARS = 200

# `[[Link Text]]` or `[[folder/note]]` or `[[note|display]]`. We pick up
# the first alt before the pipe and strip `.md`/anchors so a target
# resolves cleanly against the vault's note stems.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")


def _vault(request: Request) -> Vault:
    vault = getattr(request.app.state, "brain", None)
    if vault is None:
        raise HTTPException(status_code=503, detail="brain vault offline")
    return vault


# YAML frontmatter at the top of a note (``---\n...\n---``). Stripped
# before we hunt for a display title so the title doesn't come back as
# an opaque "title: Something" key/value line.
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)
# Markdown heading line — captures the text after the hashes.
_HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
# Cheap per-file read ceiling for title extraction. 2 KiB is plenty to
# find the first heading or first prose line on any sensibly-shaped
# markdown note.
_TITLE_READ_BYTES = 2048
# Hard cap on how long a display title can be. The card UI clamps to
# three lines but we still want to bound the payload.
_TITLE_MAX_CHARS = 120


def _looks_like_timestamp(text: str) -> bool:
    """Heuristic: does this look like a bare date / timestamp line?
    Covers ``2026-04-22``, ``2026-04-22 14:30``, ``14:30 UTC``, etc.
    These show up as the first line of daily notes and per-hour
    ingested files — useless as display titles."""
    stripped = text.strip()
    if not stripped:
        return True
    return bool(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}([ T-]\d{2}:\d{2}(:\d{2})?)?( UTC)?", stripped)
        or re.fullmatch(r"\d{1,2}:\d{2}( UTC)?", stripped)
    )


def _display_title(vault: Vault, rel: str, stem: str) -> str | None:
    """Return a human-readable title for the note at ``rel``, or
    ``None`` when we can't improve on the filename.

    We read the first ``_TITLE_READ_BYTES`` of the file, strip any
    YAML frontmatter, then return:
      1. The first markdown heading whose text isn't just a
         repetition of the filename stem or a bare timestamp.
      2. Otherwise, the first non-trivial prose line.

    Cheap enough to call on every note in the list endpoint — the
    read is bounded and the vault is on local disk.
    """
    abs_path = vault.root / rel
    try:
        with abs_path.open("rb") as fh:
            raw = fh.read(_TITLE_READ_BYTES)
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    text = _FRONTMATTER_RE.sub("", text, count=1)
    stem_lower = stem.strip().lower()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Markdown rule, blockquote, table delimiter — skip.
        if stripped.startswith(("---", ">", "|")):
            continue
        h = _HEADING_LINE_RE.match(stripped)
        if h:
            heading = h.group(1).strip()
            if not heading or heading.lower() == stem_lower:
                continue
            if _looks_like_timestamp(heading):
                continue
            return heading[:_TITLE_MAX_CHARS]
        # First prose line — strip cheap markdown emphasis markers so
        # the card doesn't render ``**bold**`` with the asterisks.
        plain = re.sub(r"[*_`]+", "", stripped)
        plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", plain)
        plain = re.sub(r"\s+", " ", plain).strip()
        if not plain or _looks_like_timestamp(plain):
            continue
        return plain[:_TITLE_MAX_CHARS]
    return None


def _list_row(vault: Vault, rel: str) -> dict[str, Any]:
    """Stat + split a vault-relative path into the shape the dashboard
    tree view expects. We tolerate stat failures quietly — a note that
    vanished between list and stat just gets zeros rather than a 500.

    Also peeks at the first few lines of the file to derive a human
    display title (first heading / first prose line), so cards show
    something more meaningful than the raw filename stem.
    """
    abs_path = vault.root / rel
    try:
        st = abs_path.stat()
        size = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat()
    except OSError:
        size = 0
        mtime = None
    folder, _, filename = rel.rpartition("/")
    stem = filename[:-3] if filename.endswith(".md") else filename
    title = _display_title(vault, rel, stem)
    return {
        "path": rel,
        "folder": folder,
        "stem": stem,
        "title": title,
        "size": size,
        "mtime": mtime,
    }


@router.get("")
async def list_notes(request: Request) -> dict[str, Any]:
    """Return every markdown file in the vault, sorted. Cheap enough to
    pull the whole tree on every dashboard open: even a vault with a
    few thousand notes is < 200 KiB of JSON on the wire."""
    vault = _vault(request)
    try:
        rels = vault.list()
    except (OSError, ValueError) as e:
        log.warning("brain_list_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e
    notes = [_list_row(vault, r) for r in rels]
    return {"notes": notes, "root": str(vault.root)}


@router.get("/note")
async def read_note(
    request: Request, path: str = Query(..., min_length=1, max_length=400)
) -> dict[str, Any]:
    vault = _vault(request)
    try:
        body = vault.read(path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (ValueError, IsADirectoryError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OSError as e:
        log.warning("brain_read_failed", path=path, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e
    # Resolve gives us the actual path on disk (after `.md` append etc.).
    try:
        rel = vault.resolve(path).relative_to(vault.root).as_posix()
    except ValueError:
        rel = path
    return {"path": rel, "body": body, "size": len(body.encode("utf-8"))}


@router.get("/search")
async def search_notes(
    request: Request,
    q: str = Query(..., min_length=SEARCH_MIN_CHARS, max_length=SEARCH_MAX_CHARS),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    vault = _vault(request)
    try:
        hits = vault.search(q, limit=limit)
    except OSError as e:
        log.warning("brain_search_failed", q=q, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {
        "query": q,
        "hits": [
            {"path": h.path, "line": h.line, "snippet": h.snippet}
            for h in hits
        ],
    }


# ── graph + backlinks ────────────────────────────────────────────────


def _extract_wikilinks(body: str) -> list[str]:
    """Return the raw target strings from every ``[[target]]`` in
    ``body``. Targets may include a folder prefix (``inbox/foo``) or
    a display alias (``foo|Display``) — we strip the alias but keep
    any folder prefix for the resolver to match against."""
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(body or ""):
        raw = m.group(1).strip()
        if raw:
            out.append(raw)
    return out


def _resolve_target(
    target: str,
    *,
    by_path: dict[str, str],
    by_stem: dict[str, list[str]],
) -> str | None:
    """Match a wiki-link string against real notes.

    Priority:
      1. Exact path match (``folder/name`` or ``folder/name.md``).
      2. Stem match (``name`` → note with that filename stem; ties
         return None so the edge is dropped rather than guessed).
    """
    if not target:
        return None
    normalized = target.strip().lstrip("/")
    if normalized.endswith(".md"):
        normalized = normalized[:-3]
    if normalized in by_path:
        return by_path[normalized]
    stem = normalized.rsplit("/", 1)[-1].lower()
    candidates = by_stem.get(stem, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _build_note_index(vault: Vault) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return two lookup tables keyed on the same canonical note paths:

      by_path  — ``"folder/name"`` (no .md) → ``"folder/name.md"``
      by_stem  — ``"name"`` (lowercased) → [full relative paths]
    """
    by_path: dict[str, str] = {}
    by_stem: dict[str, list[str]] = {}
    for rel in vault.list():
        key = rel[:-3] if rel.endswith(".md") else rel
        by_path[key] = rel
        _, _, filename = rel.rpartition("/")
        stem = (filename[:-3] if filename.endswith(".md") else filename).lower()
        by_stem.setdefault(stem, []).append(rel)
    return by_path, by_stem


@router.get("/graph")
async def graph(request: Request) -> dict[str, Any]:
    """Build a node + edge graph of the vault.

    One node per note (id = vault-relative path, e.g.
    ``inbox/note.md``). Edges come from `[[wiki-links]]` inside each
    note body. Unresolvable link targets (typos, aliases, external
    refs) are silently dropped — the client renders only edges that
    both endpoints exist for.

    Node metadata includes `folder` so the UI can colour clusters
    by origin (inbox vs daily vs ingested/docs etc.). `size` is the
    file size in bytes for radius sizing.
    """
    vault = _vault(request)
    try:
        rels = vault.list()
    except (OSError, ValueError) as e:
        log.warning("brain_graph_list_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e

    by_path, by_stem = _build_note_index(vault)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()

    for rel in rels:
        row = _list_row(vault, rel)
        nodes.append(
            {
                "id": rel,
                "label": row["stem"],
                "folder": row["folder"],
                "size": row["size"],
            }
        )
        try:
            body = vault.read(rel)
        except (OSError, ValueError, FileNotFoundError):
            # A note that vanished between list + read: skip edges
            # but keep its node (so the graph still shows it).
            continue
        for target in _extract_wikilinks(body):
            dest = _resolve_target(target, by_path=by_path, by_stem=by_stem)
            if dest is None or dest == rel:
                continue
            key = (rel, dest)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({"source": rel, "target": dest})

    return {"nodes": nodes, "edges": edges, "root": str(vault.root)}


@router.get("/backlinks")
async def backlinks(
    request: Request,
    path: str = Query(..., min_length=1, max_length=400),
) -> dict[str, Any]:
    """Return every note that contains a wiki-link resolving to
    ``path``. Powers the right-hand panel on the Brain page."""
    vault = _vault(request)
    try:
        resolved = vault.resolve(path)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if not resolved.exists():
        # vault.resolve only validates path safety, not existence —
        # backlinks on a nonexistent target is an API error, not an
        # empty-result query. 404 matches the rest of the brain route.
        raise HTTPException(
            status_code=404, detail=f"note not found: {path}",
        )
    target_rel = resolved.relative_to(vault.root).as_posix()

    by_path, by_stem = _build_note_index(vault)
    try:
        rels = vault.list()
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    out: list[dict[str, Any]] = []
    for rel in rels:
        if rel == target_rel:
            continue
        try:
            body = vault.read(rel)
        except (OSError, ValueError, FileNotFoundError):
            continue
        matched_line: int | None = None
        matched_snippet = ""
        for lineno, line in enumerate(body.splitlines(), start=1):
            for raw in _extract_wikilinks(line):
                dest = _resolve_target(raw, by_path=by_path, by_stem=by_stem)
                if dest == target_rel:
                    matched_line = lineno
                    matched_snippet = line.strip()[:200]
                    break
            if matched_line is not None:
                break
        if matched_line is not None:
            out.append(
                {
                    "path": rel,
                    "line": matched_line,
                    "snippet": matched_snippet,
                }
            )
    return {"target": target_rel, "links": out}


# ── write endpoints (upload / edit / delete) ─────────────────────────

# Cap on accepted PDF / .txt / .zip uploads. Very long-running ChatGPT
# accounts can produce 2+ GiB export zips; we cap at 3 GiB so those
# still land while still refusing something wildly oversized. The
# route streams the body to a tempfile rather than loading it into
# RAM, so a 3 GiB upload costs disk, not memory. The vault's own
# MAX_WRITE_BYTES still caps per-note size downstream, so this is
# effectively just the zip envelope cap.
MAX_UPLOAD_BYTES = 3 * 1024 * 1024 * 1024  # 3 GiB
# Chunk size used when streaming the upload body to disk. 1 MiB keeps
# the per-iteration async round-trips cheap without blowing the event
# loop on tiny writes.
_STREAM_CHUNK_BYTES = 1 * 1024 * 1024

# We use pypdf (already in pyproject.toml via the ingester pipeline)
# rather than pulling in a new dependency. The inline import below
# degrades gracefully if pypdf isn't installed in some slim env.
_SUPPORTED_UPLOAD_SUFFIXES = (".pdf", ".txt", ".zip")

# Where uploaded ChatGPT zips get archived verbatim, alongside the
# extracted per-conversation markdown notes. Having the raw export
# preserved means the operator can re-run the ingester later (e.g.
# if we add richer extraction) without asking ChatGPT for another
# export. Lives outside the vault's Obsidian-facing tree but under
# the same root so backups cover it.
_CHATGPT_ARCHIVE_DIR = "ingested/chatgpt-archive"


class NotePatch(BaseModel):
    path: str = Field(..., min_length=1, max_length=400)
    content: str = Field(..., max_length=1024 * 1024)


def _slugify_for_filename(raw: str) -> str:
    """Turn an arbitrary label or filename into something the Vault's
    strict path regex will accept. The Vault allows letters, digits,
    spaces, `_-./()'`; anything else we collapse to a dash."""
    cleaned = re.sub(r"[^A-Za-z0-9 _\-().']+", "-", (raw or "").strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-. ")
    return cleaned or "untitled"


def _unique_note_path(vault: Vault, folder: str, stem: str) -> str:
    """Pick a vault-relative path that doesn't collide with an existing
    note. Returns ``folder/stem.md`` (or with an ``-N`` suffix if taken).
    ``folder`` may be empty for a root-level note."""
    folder_clean = (folder or "").strip("/ ")
    base_stem = _slugify_for_filename(stem)
    candidate = f"{folder_clean}/{base_stem}" if folder_clean else base_stem
    try:
        resolved = vault.resolve(candidate)
    except ValueError:
        # Fall back to a known-safe stem if slug still rejected.
        base_stem = _slugify_for_filename(base_stem + " upload")
        candidate = f"{folder_clean}/{base_stem}" if folder_clean else base_stem
        resolved = vault.resolve(candidate)
    if not resolved.exists():
        return resolved.relative_to(vault.root).as_posix()
    for i in range(2, 1000):
        suffixed = f"{candidate} {i}"
        resolved = vault.resolve(suffixed)
        if not resolved.exists():
            return resolved.relative_to(vault.root).as_posix()
    raise HTTPException(status_code=409, detail="cannot allocate unique note path")


def _archive_chatgpt_zip(
    vault: Vault, source_zip: Path, filename: str
) -> str:
    """Copy the uploaded zip into the vault's chatgpt-archive folder
    verbatim so the operator has a bit-for-bit backup of the original
    ChatGPT export. Returns the archive path relative to the vault
    root — caller surfaces it in the response for a "saved to …"
    toast in the UI.

    Timestamped so re-uploads never clobber each other. Sanitised
    stem because the Vault's path rules only allow a safe character
    class, and we want this to live next to the Obsidian-native
    notes rather than in some untouchable sibling dir.
    """
    now = datetime.now(UTC)
    base_stem = Path(filename or "chatgpt-export").stem
    safe_stem = _slugify_for_filename(base_stem)[:60] or "export"
    archive_dir = vault.root / _CHATGPT_ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest_name = f"{now.strftime('%Y-%m-%d-%H%M%S')}-{safe_stem}.zip"
    dest = archive_dir / dest_name
    # copyfile preserves the tempfile so the caller's unlink still
    # runs cleanly; we accept the duplicated disk write because it's
    # the price of an independent backup copy.
    shutil.copyfile(source_zip, dest)
    rel = dest.relative_to(vault.root).as_posix()
    log.info(
        "brain_upload_chatgpt_archived",
        source=filename or "chatgpt.zip",
        archive_path=rel,
        bytes=dest.stat().st_size,
    )
    return rel


def _ingest_chatgpt_zip_path(
    vault: Vault, zip_path: Path, filename: str
) -> list[dict[str, Any]]:
    """Import a ChatGPT "Export data" zip (already on disk) as a batch
    of vault notes.

    ChatGPT's export is a zip with ``conversations.json`` at the root.
    We write each conversation to ``ingested/chatgpt/<date>-<slug>.md``
    using the same renderer the auto-ingest pipeline uses, so manual
    uploads and cron-driven re-syncs land in identical shapes. A
    single malformed conversation is skipped rather than aborting the
    whole import — the operator would rather see 299 / 300 than zero.
    """
    # Lazy import so the route module stays importable on envs where
    # the ingester's optional deps aren't installed.
    from core.integrations.ingesters.chatgpt import (
        ChatGPTIngestError,
        parse_export,
        render_conversation_note,
    )

    try:
        conversations = parse_export(zip_path)
    except ChatGPTIngestError as e:
        raise HTTPException(
            status_code=400,
            detail=(
                "Zip doesn't look like a ChatGPT export — expected "
                f"`conversations.json` (or sharded `conversations-NNN.json`) "
                f"inside ({e})."
            ),
        ) from e
    if not conversations:
        raise HTTPException(
            status_code=400,
            detail="ChatGPT export contains zero conversations.",
        )

    written: list[dict[str, Any]] = []
    skipped = 0
    for conv in conversations:
        rendered = render_conversation_note(conv)
        try:
            vault.write(rendered.path, rendered.body)
        except (OSError, ValueError) as e:
            log.warning(
                "brain_upload_chatgpt_write_failed",
                path=rendered.path, error=str(e),
            )
            skipped += 1
            continue
        written.append(_list_row(vault, rendered.path))

    log.info(
        "brain_upload_chatgpt_saved",
        source=filename or "chatgpt.zip",
        total=len(conversations),
        written=len(written),
        skipped=skipped,
    )
    return written


async def _stream_upload_to_tempfile(
    file: UploadFile, *, suffix: str
) -> Path:
    """Stream ``file`` to a named tempfile, enforcing ``MAX_UPLOAD_BYTES``
    as we go. Returns the tempfile path; caller is responsible for
    unlinking it. Raises ``HTTPException`` on oversized or empty
    uploads.

    Streaming matters for ChatGPT exports in the multi-GiB range — the
    previous ``await file.read()`` path held the entire body in RAM,
    which would OOM pilkd on a 2 GiB upload from a laptop with modest
    memory.
    """
    # Deliberately not a ``with`` block: we want the file to outlive
    # this function so the caller can hand the path to pypdf / the
    # ChatGPT ingester. Unlink happens in the caller's ``finally``.
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
        suffix=suffix or ".bin",
        prefix="brain-upload-",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    total = 0
    try:
        while True:
            chunk = await file.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                tmp.close()
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
                cap_gib = MAX_UPLOAD_BYTES / (1024 ** 3)
                raise HTTPException(
                    status_code=413,
                    detail=f"upload exceeds {cap_gib:.1f} GiB cap",
                )
            tmp.write(chunk)
    except HTTPException:
        raise
    except Exception:
        tmp.close()
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    else:
        tmp.close()
    if total == 0:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise HTTPException(status_code=400, detail="empty upload")
    return tmp_path


def _extract_pdf_text_from_stream(stream: Any) -> str:
    """Extract concatenated page text from a PDF byte stream.

    Takes any file-like with ``.read()`` / ``.seek()`` semantics so we
    can hand pypdf a file handle straight from the upload tempfile —
    avoids materialising the whole PDF into a ``BytesIO`` for large
    documents.
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="pypdf is not installed; cannot extract PDF text",
        ) from e
    try:
        reader = PdfReader(stream)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"could not open PDF: {e}",
        ) from e
    parts: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        text = text.strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


@router.post("/upload")
async def upload_note(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008 — FastAPI dependency pattern
    label: str = Form(""),
    folder: str = Form(""),
) -> dict[str, Any]:
    """Accept a PDF, .txt, or .zip upload and land it in the vault.

    * ``.pdf`` / ``.txt`` → extract text, write one note. ``label`` is
      the note title; ``folder`` is the vault-relative destination.
    * ``.zip`` → currently recognises ChatGPT "Export data" zips (the
      ones containing ``conversations.json``). Each conversation
      becomes one markdown note under ``ingested/chatgpt/``, named by
      date + slug. ``label`` / ``folder`` are ignored because the
      ingester owns the destination shape.

    Response is always ``{"notes": [BrainNote, ...]}`` so the client
    handles single-note uploads and zip imports uniformly.
    """
    vault = _vault(request)
    filename = (file.filename or "").strip()
    filename_lower = filename.lower()
    suffix_ok = filename_lower.endswith(_SUPPORTED_UPLOAD_SUFFIXES)
    mime = (file.content_type or "").lower()
    mime_ok = mime in {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "application/zip",
        "application/x-zip-compressed",
    }
    if not (suffix_ok or mime_ok):
        raise HTTPException(
            status_code=415,
            detail=(
                "unsupported file type — upload a PDF, .txt, or .zip "
                "(ChatGPT export) file."
            ),
        )

    # Stream the body to disk (bounded by MAX_UPLOAD_BYTES) so a
    # multi-GiB ChatGPT export doesn't get pulled into RAM. Callers
    # of each branch below read from the tempfile, not from a bytes
    # buffer, and we unlink on the way out.
    is_zip = filename_lower.endswith(".zip") or mime in (
        "application/zip",
        "application/x-zip-compressed",
    )
    is_pdf = filename_lower.endswith(".pdf") or mime == "application/pdf"
    suffix = ".zip" if is_zip else ".pdf" if is_pdf else ".txt"
    tmp_path = await _stream_upload_to_tempfile(file, suffix=suffix)

    try:
        # ── .zip branch (ChatGPT export for now) ──────────────
        if is_zip:
            # Archive the raw zip verbatim BEFORE parsing so a crash
            # in the ingester still leaves the operator with an
            # untouched backup of their export. Archive failures are
            # fatal (we'd rather tell the operator we couldn't save
            # than pretend we did).
            archive_rel = _archive_chatgpt_zip(vault, tmp_path, filename)
            notes = _ingest_chatgpt_zip_path(vault, tmp_path, filename)
            return {
                "notes": notes,
                "source_kind": "chatgpt_export",
                "imported": len(notes),
                "archive_path": archive_rel,
            }

        # ── .pdf / .txt branches ─────────────────────────────
        if is_pdf:
            # pypdf reads from a file-like object; open the tempfile
            # in binary mode rather than loading it all into memory.
            with tmp_path.open("rb") as fh:
                body_text = _extract_pdf_text_from_stream(fh)
            if not body_text.strip():
                # Still write a stub so the operator sees their upload
                # landed and can add context manually. Matches the
                # docs ingester's behaviour for image-only PDFs.
                body_text = "_(PDF text extraction produced no content — file may be scanned / image-only.)_"
        else:
            raw = tmp_path.read_bytes()
            body_text = None
            for encoding in ("utf-8", "latin-1"):
                try:
                    body_text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            if body_text is None:
                raise HTTPException(
                    status_code=400,
                    detail="text file is not utf-8 or latin-1",
                )

        clean_label = (label or "").strip() or (
            filename.rsplit(".", 1)[0] if filename else "upload"
        )
        rel_path = _unique_note_path(vault, folder, clean_label)

        source_note = (
            f"> Uploaded from **{filename or 'upload'}** "
            f"on {datetime.now(tz=UTC).strftime('%Y-%m-%d')}."
        )
        markdown = f"# {clean_label}\n\n{source_note}\n\n{body_text.rstrip()}\n"

        try:
            vault.write(rel_path, markdown)
        except (OSError, ValueError) as e:
            log.warning("brain_upload_write_failed", path=rel_path, error=str(e))
            raise HTTPException(status_code=400, detail=str(e)) from e

        source_kind = "pdf" if is_pdf else "text"
        log.info(
            "brain_upload_saved",
            path=rel_path,
            bytes=len(markdown.encode("utf-8")),
            source_kind=source_kind,
        )
        return {
            "notes": [_list_row(vault, rel_path)],
            "source_kind": source_kind,
            "imported": 1,
        }
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()


@router.patch("/note")
async def update_note(request: Request, patch: NotePatch) -> dict[str, Any]:
    """Overwrite the body of an existing note. The UI sends the full
    new body (not a diff) — matches how the agent's write tool works
    and keeps the vault side stateless."""
    vault = _vault(request)
    try:
        resolved = vault.resolve(patch.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"note not found: {patch.path}")
    try:
        vault.write(patch.path, patch.content)
    except (OSError, ValueError) as e:
        log.warning("brain_patch_failed", path=patch.path, error=str(e))
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        rel = resolved.relative_to(vault.root).as_posix()
    except ValueError:
        rel = patch.path
    return {"note": _list_row(vault, rel)}


@router.delete("/note")
async def delete_note(
    request: Request, path: str = Query(..., min_length=1, max_length=400)
) -> dict[str, Any]:
    vault = _vault(request)
    try:
        resolved = vault.resolve(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"note not found: {path}")
    try:
        resolved.unlink()
    except OSError as e:
        log.warning("brain_delete_failed", path=path, error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e
    log.info("brain_note_deleted", path=path)
    return {"deleted": True, "path": path}
