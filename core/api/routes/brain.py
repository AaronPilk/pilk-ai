"""HTTP surface for the long-term Obsidian brain vault.

  GET    /brain                       list every note in the vault
  GET    /brain/note?path=folder/x    read one note (markdown body)
  GET    /brain/search?q=…            substring search across notes

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

so the UI can render its own tree without a second round-trip.
"""

from __future__ import annotations

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
