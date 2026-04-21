"""macOS Contacts — read via AppleScript.

Talks to the local Contacts.app through an ``osascript`` bridge,
mirroring the Messages tool shape. No OAuth, no Google People API —
the operator's laptop is the source of truth.

### Permission posture

macOS 10.14+ gates every Contacts read behind the Automation (and
sometimes dedicated Contacts) privacy permission. The first call
pops a system dialog asking the operator to grant the terminal /
pilkd process access. If denied, the tool surfaces a friendly
"grant Automation access" message — same wording as the Messages
send tool.

### Why AppleScript, not the `Contacts` CLI

The Homebrew `contacts` CLI covers some of this but is not
universally installed. AppleScript + osascript ship with every Mac
and can fully enumerate the fields we care about (name, emails,
phones, organization). We pay the subprocess round-trip; the tool
isn't in a hot path.

### What we return

For each matching person:

- ``name``       full name (first + last joined)
- ``emails``     list of email addresses (can be empty)
- ``phones``     list of phone numbers (can be empty)
- ``company``    organization string or ``""``

No address, no birthday, no note fields — easy to extend later if a
workflow needs them.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from typing import Any

from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.apple.contacts")

# Cap returned matches so a three-letter query doesn't dump thousands
# of contacts into a planner turn.
MAX_CONTACT_RESULTS = 25
OSASCRIPT_TIMEOUT_S = 15.0

# Field separator used inside the AppleScript "one-record-per-line"
# output. Picked to be something unlikely to appear in a name / email
# / phone / company string. If someone ever has a literal U+001F in
# their company name we'll re-pick.
_FIELD_SEP = "\x1f"
# Repeatable sub-field separator for emails + phones (one person can
# have several of each).
_SUB_SEP = "\x1e"
# Row separator — a literal newline would get munged by AppleScript's
# string coercion in some macOS versions, so we pick U+001D.
_ROW_SEP = "\x1d"


class ContactsSearchError(RuntimeError):
    """Raised when the AppleScript contact fetch fails. Wraps the
    underlying subprocess failure or friendly reason."""


def _run_osascript(script: str, *args: str) -> str:
    if sys.platform != "darwin":
        raise ContactsSearchError(
            "contacts_search is macOS-only (uses the local Contacts "
            "app via AppleScript)."
        )
    binary = shutil.which("osascript")
    if binary is None:
        raise ContactsSearchError("osascript binary not found on PATH.")
    try:
        proc = subprocess.run(
            [binary, "-", *args],
            input=script,
            capture_output=True,
            text=True,
            timeout=OSASCRIPT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ContactsSearchError(
            f"osascript timed out after {OSASCRIPT_TIMEOUT_S}s — "
            "macOS is probably prompting for permission. Allow the "
            "pilkd / terminal process in System Settings → Privacy & "
            "Security → Automation (and Contacts if prompted)."
        ) from e
    except OSError as e:
        raise ContactsSearchError(f"osascript failed to launch: {e}") from e
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if "not authorised" in stderr.lower() or "-1743" in stderr:
            hint = (
                "macOS refused access to Contacts. Open System Settings "
                "→ Privacy & Security → Automation and allow pilkd / "
                "your terminal to control Contacts."
            )
        else:
            hint = stderr or f"osascript exit {proc.returncode}"
        raise ContactsSearchError(hint)
    return proc.stdout or ""


# ── AppleScript ─────────────────────────────────────────────────────────
#
# Outputs rows separated by ``_ROW_SEP``. Inside each row, fields are
# separated by ``_FIELD_SEP``. Multi-valued fields (emails, phones) are
# internally separated by ``_SUB_SEP``. Keeping field order stable
# (name, emails, phones, company) so the Python-side parser is a plain
# ``.split()``.

_CONTACTS_SCRIPT = """on run argv
    set q to item 1 of argv
    set maxResults to (item 2 of argv) as integer
    set rowSep to character id 29
    set fieldSep to character id 31
    set subSep to character id 30
    set out to ""
    tell application "Contacts"
        set matches to (every person whose name contains q)
        set seen to 0
        repeat with p in matches
            if seen >= maxResults then exit repeat
            set seen to seen + 1
            set nm to (name of p) as text
            set emailList to ""
            try
                repeat with e in (every email of p)
                    if emailList is not "" then
                        set emailList to emailList & subSep
                    end if
                    set emailList to emailList & ((value of e) as text)
                end repeat
            end try
            set phoneList to ""
            try
                repeat with ph in (every phone of p)
                    if phoneList is not "" then
                        set phoneList to phoneList & subSep
                    end if
                    set phoneList to phoneList & ((value of ph) as text)
                end repeat
            end try
            set orgText to ""
            try
                set orgText to (organization of p) as text
            end try
            if orgText is missing value then set orgText to ""
            set row to nm & fieldSep & emailList & fieldSep & phoneList & fieldSep & orgText
            if out is not "" then set out to out & rowSep
            set out to out & row
        end repeat
    end tell
    return out
end run
"""


def _parse_contacts(raw: str) -> list[dict[str, Any]]:
    """Decode the AppleScript payload into a list of contact dicts.

    Defensive against an empty response, trailing row separators, and
    records that are missing trailing fields (happens on contacts
    with no company).
    """
    if not raw:
        return []
    out: list[dict[str, Any]] = []
    for row in raw.split(_ROW_SEP):
        row = row.strip()
        if not row:
            continue
        parts = row.split(_FIELD_SEP)
        # Pad so we can index safely regardless of trailing blanks.
        while len(parts) < 4:
            parts.append("")
        name, emails_raw, phones_raw, company = parts[:4]
        emails = [e for e in emails_raw.split(_SUB_SEP) if e]
        phones = [p for p in phones_raw.split(_SUB_SEP) if p]
        out.append(
            {
                "name": name,
                "emails": emails,
                "phones": phones,
                "company": company,
            }
        )
    return out


def search_contacts(
    query: str, *, limit: int = MAX_CONTACT_RESULTS,
) -> list[dict[str, Any]]:
    clamped = max(1, min(int(limit), MAX_CONTACT_RESULTS))
    raw = _run_osascript(_CONTACTS_SCRIPT, query, str(clamped))
    return _parse_contacts(raw)


# ── tool factory ────────────────────────────────────────────────────────


def make_contacts_tools() -> list[Tool]:
    async def _search(args: dict, ctx: ToolContext) -> ToolOutcome:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolOutcome(
                content="contacts_search requires a non-empty 'query'.",
                is_error=True,
            )
        try:
            raw_limit = int(args.get("max_results") or MAX_CONTACT_RESULTS)
        except (TypeError, ValueError):
            raw_limit = MAX_CONTACT_RESULTS
        try:
            results = await asyncio.to_thread(
                search_contacts, query, limit=raw_limit,
            )
        except ContactsSearchError as e:
            return ToolOutcome(
                content=f"contacts_search failed: {e}", is_error=True,
            )
        except Exception as e:
            log.exception("contacts_search_unexpected_error")
            return ToolOutcome(
                content=(
                    f"contacts_search failed: {type(e).__name__}: {e}"
                ),
                is_error=True,
            )
        if not results:
            return ToolOutcome(
                content=f"No contacts match {query!r}.",
                data={"query": query, "results": []},
            )
        lines = []
        for r in results:
            bits = [r["name"]]
            if r["company"]:
                bits.append(f"({r['company']})")
            if r["emails"]:
                bits.append(f"· {', '.join(r['emails'])}")
            if r["phones"]:
                bits.append(f"· {', '.join(r['phones'])}")
            lines.append(" ".join(bits))
        header = f"{len(results)} contact(s) match {query!r}:\n\n"
        return ToolOutcome(
            content=header + "\n".join(lines),
            data={"query": query, "results": results},
        )

    return [
        Tool(
            name="contacts_search",
            description=(
                "Search the operator's local macOS Contacts by name "
                "substring. Returns name, emails, phones, and company "
                "for each match (up to 25). Use before sending email "
                "or iMessage when you only have a person's name and "
                "need the address / number. READ risk — local only, "
                "no network. macOS-only; needs Automation permission "
                "for Contacts granted to pilkd on first call."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Substring to match against full name. "
                            "Case-insensitive (Contacts.app does its "
                            "own casefolding)."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_CONTACT_RESULTS,
                    },
                },
                "required": ["query"],
            },
            risk=RiskClass.READ,
            handler=_search,
        )
    ]


__all__ = [
    "MAX_CONTACT_RESULTS",
    "ContactsSearchError",
    "make_contacts_tools",
    "search_contacts",
]
