"""HTTP surface for the long-term Obsidian brain vault.

  GET    /brain                       list every note in the vault
  GET    /brain/note?path=folder/x    read one note (markdown body)
  GET    /brain/search?q=…            substring search across notes
  GET    /brain/graph                 {nodes, edges} from every note's
                                      wiki-links — feeds the dashboard
                                      force-directed graph view.
  GET    /brain/backlinks?path=…      which other notes link to this one

The vault is a plain folder of markdown files under
``PILK_BRAIN_VAULT_PATH`` (default ``~/PILK-brain``). Opening the same
folder in Obsidian gives graph + backlink navigation on exactly the
same files the dashboard reads through here. The endpoints are all
read-only — writing happens via the `brain_note_write` tool inside the
agent loop, which flows through the same Vault object and therefore
the same safety checks (path escape guards, size caps, atomic writes).

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

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

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


def _list_row(vault: Vault, rel: str) -> dict[str, Any]:
    """Stat + split a vault-relative path into the shape the dashboard
    tree view expects. We tolerate stat failures quietly — a note that
    vanished between list and stat just gets zeros rather than a 500."""
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
    return {
        "path": rel,
        "folder": folder,
        "stem": stem,
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
