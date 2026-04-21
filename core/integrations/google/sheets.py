"""Google Sheets tool factory.

Two tools, mirrored on the slides.py pattern:

- ``sheets_create`` — create a brand-new spreadsheet in the operator's
  Drive, optionally seeding the first sheet with a header row.
  Returns the spreadsheet id + shareable URL.
- ``sheets_append_rows`` — append rows to an existing sheet. One API
  call regardless of row count, so a prospecting agent can batch a
  whole lead list in a single tool invocation.

Both bind to the operator's ``user`` Google account (same pattern as
Gmail / Drive / Slides) and run the blocking Google client through
``asyncio.to_thread`` so they don't hog the event loop.

Scope group: ``sheets`` (unlocks ``spreadsheets`` + ``drive.file``).
The user must widen the scope group on their Google link in
Settings → Connected accounts before these tools can write.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.identity import AccountsStore
from core.integrations.google.oauth import credentials_from_blob
from core.policy.risk import RiskClass
from core.tools.registry import AccountBinding, Tool, ToolContext, ToolOutcome

log = logging.getLogger(__name__)

_SHEETS_ROLE = "user"

# Google Sheets has no documented row cap on the values append endpoint
# (the 10 MiB payload cap is what actually bites), but we keep a sane
# per-call bound so a runaway agent can't pour 50k rows into one call.
MAX_APPEND_ROWS = 5_000
MAX_TITLE_CHARS = 100
MAX_TAB_NAME_CHARS = 100


def make_sheets_tools(accounts: AccountsStore) -> list[Tool]:
    """Factory mirroring :func:`make_slides_tools`. Returns two tools."""
    binding = AccountBinding(provider="google", role=_SHEETS_ROLE)

    def _load_creds():
        account = accounts.resolve_binding(binding)
        if account is None:
            return None, None
        tokens = accounts.load_tokens(account.account_id)
        if tokens is None:
            return None, account
        blob = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "client_id": tokens.client_id,
            "client_secret": tokens.client_secret,
            "scopes": tokens.scopes,
            "token_uri": tokens.token_uri,
            "email": account.email,
        }
        return credentials_from_blob(blob), account

    _not_linked = ToolOutcome(
        content=(
            "No Google account connected. Open Settings → Connected "
            "accounts and link a Google account with the Sheets scope."
        ),
        is_error=True,
    )

    # ── sheets_create ───────────────────────────────────────────

    async def _create(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds, _account = _load_creds()
        if creds is None:
            return _not_linked

        title = (args.get("title") or "").strip()
        if not title:
            return ToolOutcome(
                content="sheets_create requires a non-empty title.",
                is_error=True,
            )
        if len(title) > MAX_TITLE_CHARS:
            return ToolOutcome(
                content=(
                    f"sheets_create title too long ({len(title)} > "
                    f"{MAX_TITLE_CHARS}). Google caps titles; shorten it."
                ),
                is_error=True,
            )
        header = args.get("header") or []
        if header and not isinstance(header, list):
            return ToolOutcome(
                content="sheets_create header must be a list of strings.",
                is_error=True,
            )
        if header and not all(isinstance(c, str) for c in header):
            return ToolOutcome(
                content="sheets_create header must contain only strings.",
                is_error=True,
            )

        try:
            result = await asyncio.to_thread(
                _do_create, creds, title=title, header=list(header),
            )
        except Exception as e:
            log.exception("sheets_create_failed")
            return ToolOutcome(
                content=f"sheets_create failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        return ToolOutcome(
            content=(
                f"Created '{title}' "
                f"({'with' if header else 'without'} header). "
                f"{result['url']}"
            ),
            data=result,
        )

    sheets_create_tool = Tool(
        name="sheets_create",
        description=(
            "Create a new Google Sheet in the operator's Drive with an "
            "optional header row. Returns spreadsheet_id + shareable URL. "
            "Use before sheets_append_rows when you need a fresh sheet "
            "for a batch of output (e.g. a prospect list)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Sheet title — what the operator sees in Drive."
                    ),
                },
                "header": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional header row written to row 1. Typical "
                        "use: ['name', 'website', 'email', 'score']."
                    ),
                },
            },
            "required": ["title"],
        },
        risk=RiskClass.NET_WRITE,
        handler=_create,
    )

    # ── sheets_append_rows ──────────────────────────────────────

    async def _append(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds, _account = _load_creds()
        if creds is None:
            return _not_linked

        spreadsheet_id = (args.get("spreadsheet_id") or "").strip()
        if not spreadsheet_id:
            return ToolOutcome(
                content=(
                    "sheets_append_rows requires a spreadsheet_id. "
                    "Call sheets_create first (or paste the id from "
                    "an existing sheet URL)."
                ),
                is_error=True,
            )
        tab_name = (args.get("tab_name") or "Sheet1").strip()
        if len(tab_name) > MAX_TAB_NAME_CHARS:
            return ToolOutcome(
                content=(
                    f"sheets_append_rows tab_name too long "
                    f"({len(tab_name)} > {MAX_TAB_NAME_CHARS})."
                ),
                is_error=True,
            )
        rows = args.get("rows")
        if not isinstance(rows, list) or not rows:
            return ToolOutcome(
                content=(
                    "sheets_append_rows requires a non-empty 'rows' "
                    "list — each row is a list of cells."
                ),
                is_error=True,
            )
        if len(rows) > MAX_APPEND_ROWS:
            return ToolOutcome(
                content=(
                    f"sheets_append_rows got {len(rows)} rows (max "
                    f"{MAX_APPEND_ROWS} per call). Split into batches."
                ),
                is_error=True,
            )
        for i, row in enumerate(rows):
            if not isinstance(row, list):
                return ToolOutcome(
                    content=(
                        f"sheets_append_rows row {i} is not a list "
                        "— each row must be an array of cell values."
                    ),
                    is_error=True,
                )

        try:
            result = await asyncio.to_thread(
                _do_append,
                creds,
                spreadsheet_id=spreadsheet_id,
                tab_name=tab_name,
                rows=rows,
            )
        except Exception as e:
            log.exception("sheets_append_rows_failed")
            return ToolOutcome(
                content=(
                    f"sheets_append_rows failed: {type(e).__name__}: {e}"
                ),
                is_error=True,
            )

        return ToolOutcome(
            content=(
                f"Appended {result['rows_appended']} row(s) to "
                f"'{tab_name}'."
            ),
            data=result,
        )

    sheets_append_rows_tool = Tool(
        name="sheets_append_rows",
        description=(
            "Append rows to an existing Google Sheet. Preserves existing "
            "data; rows land after the last populated row on the named "
            "tab. Batch the full list into one call — don't loop per row."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The sheet id returned by sheets_create, or the "
                        "id segment of an existing sheet URL."
                    ),
                },
                "tab_name": {
                    "type": "string",
                    "description": (
                        "Tab/sheet name to append to. Defaults to "
                        "'Sheet1' — matches the default tab Google "
                        "creates with every new sheet."
                    ),
                },
                "rows": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "array",
                        "items": {},
                    },
                    "description": (
                        "Array of rows; each row is an array of cell "
                        "values. Strings, numbers, and booleans all "
                        "round-trip cleanly."
                    ),
                },
            },
            "required": ["spreadsheet_id", "rows"],
        },
        risk=RiskClass.NET_WRITE,
        handler=_append,
    )

    return [sheets_create_tool, sheets_append_rows_tool]


# ── synchronous Google API helpers (run in a thread) ────────────


def _do_create(
    creds: Any, *, title: str, header: list[str],
) -> dict[str, Any]:
    service = creds.build("sheets", "v4")
    body: dict[str, Any] = {"properties": {"title": title}}
    created = service.spreadsheets().create(body=body).execute()
    spreadsheet_id = created["spreadsheetId"]
    if header:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="Sheet1!A1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()
    return {
        "spreadsheet_id": spreadsheet_id,
        "url": (
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        ),
        "title": title,
        "header": header,
    }


def _do_append(
    creds: Any,
    *,
    spreadsheet_id: str,
    tab_name: str,
    rows: list[list[Any]],
) -> dict[str, Any]:
    service = creds.build("sheets", "v4")
    # The A1-style range ``<tab>!A1`` combined with append + INSERT_ROWS
    # lands the rows after the last populated row on that tab, which
    # is what a prospecting agent expects.
    resp = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_name}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        )
        .execute()
    )
    return {
        "spreadsheet_id": spreadsheet_id,
        "tab_name": tab_name,
        "rows_appended": len(rows),
        "updated_range": resp.get("updates", {}).get("updatedRange"),
        "updated_rows": resp.get("updates", {}).get("updatedRows"),
    }


__all__ = [
    "MAX_APPEND_ROWS",
    "MAX_TAB_NAME_CHARS",
    "MAX_TITLE_CHARS",
    "make_sheets_tools",
]
