"""Google Drive tools, user-role only for now.

Two read-only tools bound to the user's default Google account:

- drive_search_my_files  — find files by name/type fragment (NET_READ)
- drive_read_my_file     — fetch one file's text content (NET_READ)

Write + share tools are deferred. PILK's system role intentionally has
no Drive access; the user role needs drive.readonly which it picks up
only when the user opts into the `drive` scope group when linking or
re-linking their Google account.
"""

from __future__ import annotations

import asyncio

from core.identity import AccountsStore
from core.integrations.google.oauth import credentials_from_blob
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import AccountBinding, Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.google.drive")

MAX_RESULTS = 25
DEFAULT_RESULTS = 10
MAX_FILE_CHARS = 8000


def make_drive_tools(accounts: AccountsStore) -> list[Tool]:
    """Build the user-role Drive tool set bound to the store."""
    binding = AccountBinding(provider="google", role="user")

    def _load_creds():
        account = accounts.resolve_binding(binding)
        if account is None:
            return None, None
        tokens = accounts.load_tokens(account.account_id)
        if tokens is None:
            return None, account
        return credentials_from_blob(
            {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "client_id": tokens.client_id,
                "client_secret": tokens.client_secret,
                "scopes": tokens.scopes,
                "token_uri": tokens.token_uri,
                "email": account.email,
            }
        ), account

    not_linked = ToolOutcome(
        content=(
            "Drive access isn't connected yet. Open Settings → Connected "
            "accounts, Expand access on your Google account, and enable "
            "Drive."
        ),
        is_error=True,
    )

    async def _search(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds, _account = _load_creds()
        if creds is None:
            return not_linked
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolOutcome(
                content="drive_search_my_files requires a 'query'.",
                is_error=True,
            )
        try:
            max_results = int(args.get("max_results") or DEFAULT_RESULTS)
        except (TypeError, ValueError):
            max_results = DEFAULT_RESULTS
        max_results = max(1, min(max_results, MAX_RESULTS))
        try:
            results = await asyncio.to_thread(
                _do_search, creds, query, max_results
            )
        except Exception as e:
            log.exception("drive_search_failed")
            return ToolOutcome(
                content=f"drive_search_my_files failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        lines = [
            f"{r['name'][:80]:<80}  ·  {r['mime_type'][:30]:<30}  [{r['id']}]"
            for r in results
        ]
        header = f"{len(results)} file(s) matching {query!r}:\n\n"
        return ToolOutcome(
            content=header + "\n".join(lines) if lines else f"No files match {query!r}.",
            data={"query": query, "results": results},
        )

    async def _read(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds, _account = _load_creds()
        if creds is None:
            return not_linked
        file_id = str(args.get("file_id") or "").strip()
        if not file_id:
            return ToolOutcome(
                content="drive_read_my_file requires a 'file_id'.",
                is_error=True,
            )
        try:
            info = await asyncio.to_thread(_do_read, creds, file_id)
        except Exception as e:
            log.exception("drive_read_failed")
            return ToolOutcome(
                content=f"drive_read_my_file failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        body = info["body"]
        if len(body) > MAX_FILE_CHARS:
            body = body[:MAX_FILE_CHARS].rstrip() + "…"
        return ToolOutcome(
            content=(
                f"Name: {info['name']}\nMime: {info['mime_type']}\n"
                f"Modified: {info['modified_time']}\n\n{body}"
            ),
            data=info,
        )

    search_tool = Tool(
        name="drive_search_my_files",
        description=(
            "Search your Google Drive for files by name or type fragment. "
            "Uses Drive's native query syntax (e.g. "
            "\"name contains 'contract' and mimeType = 'application/pdf'\" "
            "or a simple phrase). Returns up to 25 results with id, name, "
            "mime type. Use drive_read_my_file to pull contents."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Drive query or free-text fragment. Free text is "
                        "wrapped as `name contains '...'` automatically."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESULTS,
                },
            },
            "required": ["query"],
        },
        risk=RiskClass.NET_READ,
        handler=_search,
        account_binding=binding,
    )
    read_tool = Tool(
        name="drive_read_my_file",
        description=(
            "Fetch one Drive file by id. Google Docs / Sheets / Slides are "
            "exported as plain text. Binary files (images, PDFs) return "
            "metadata only. Body is trimmed to ~8 KB — ask a helper model "
            "to summarize longer docs."
        ),
        input_schema={
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
        },
        risk=RiskClass.NET_READ,
        handler=_read,
        account_binding=binding,
    )
    return [search_tool, read_tool]


# ── synchronous Drive helpers ───────────────────────────────────────────


def _do_search(creds, query: str, max_results: int) -> list[dict]:
    service = creds.build("drive", "v3")
    q = query if _looks_like_drive_query(query) else f"name contains '{_escape(query)}'"
    listing = (
        service.files()
        .list(
            q=q,
            pageSize=max_results,
            fields="files(id, name, mimeType, modifiedTime, owners(emailAddress))",
            corpora="user",
        )
        .execute()
    )
    out: list[dict] = []
    for f in listing.get("files", []) or []:
        owners = [o.get("emailAddress") for o in (f.get("owners") or [])]
        out.append(
            {
                "id": f.get("id", ""),
                "name": f.get("name", ""),
                "mime_type": f.get("mimeType", ""),
                "modified_time": f.get("modifiedTime", ""),
                "owners": [o for o in owners if o],
            }
        )
    return out


def _do_read(creds, file_id: str) -> dict:
    service = creds.build("drive", "v3")
    meta = (
        service.files()
        .get(fileId=file_id, fields="id, name, mimeType, modifiedTime")
        .execute()
    )
    mime = meta.get("mimeType", "")
    body = ""
    # Google native docs → export as plain text.
    native_exports = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }
    export_mime = native_exports.get(mime)
    if export_mime:
        try:
            raw = (
                service.files()
                .export(fileId=file_id, mimeType=export_mime)
                .execute()
            )
            body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        except Exception:
            body = ""
    elif mime.startswith("text/") or mime == "application/json":
        try:
            raw = (
                service.files()
                .get_media(fileId=file_id)
                .execute()
            )
            body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        except Exception:
            body = ""
    return {
        "id": meta.get("id", file_id),
        "name": meta.get("name", ""),
        "mime_type": mime,
        "modified_time": meta.get("modifiedTime", ""),
        "body": body,
    }


def _looks_like_drive_query(q: str) -> bool:
    # Drive's query language uses operators like `contains`, `=`, `!=`, `and`,
    # and attribute names like mimeType, name, parents. If the caller wrote
    # any of these, pass the query through verbatim.
    markers = (" contains ", " = ", " != ", " and ", "mimeType", "parents", "modifiedTime")
    return any(m in q for m in markers)


def _escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("'", "\\'")
