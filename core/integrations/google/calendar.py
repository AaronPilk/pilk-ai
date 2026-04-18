"""Google Calendar tools, user-role only.

Two tools bound to the user's default Google account:

- calendar_read_my_today  — today's events on the primary calendar
                             (NET_READ).
- calendar_create_my_event — create an event on the primary calendar
                             (NET_WRITE — always hits approval).

The user opts into these by enabling the `calendar` scope group when
linking or re-linking their Google account.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta

from core.identity import AccountsStore
from core.integrations.google.oauth import credentials_from_blob
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import AccountBinding, Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.google.calendar")

MAX_EVENTS = 50


def make_calendar_tools(accounts: AccountsStore) -> list[Tool]:
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
            "Calendar access isn't connected yet. Open Settings → "
            "Connected accounts, Expand access on your Google account, "
            "and enable Calendar."
        ),
        is_error=True,
    )

    async def _read_today(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds, _account = _load_creds()
        if creds is None:
            return not_linked
        date_str = str(args.get("date") or "").strip()
        try:
            events = await asyncio.to_thread(_do_read_today, creds, date_str)
        except Exception as e:
            log.exception("calendar_read_today_failed")
            return ToolOutcome(
                content=f"calendar_read_my_today failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        if not events:
            return ToolOutcome(
                content=(
                    "No events on your primary calendar for "
                    f"{date_str or 'today'}."
                ),
                data={"date": date_str, "events": []},
            )
        lines = [
            f"{e['start']}  →  {e['end']}   {e['summary']}"
            + (f"   (with {', '.join(e['attendees'])})" if e["attendees"] else "")
            for e in events
        ]
        header = (
            f"{len(events)} event(s) on "
            f"{date_str or 'today'}:\n\n"
        )
        return ToolOutcome(
            content=header + "\n".join(lines),
            data={"date": date_str, "events": events},
        )

    async def _create_event(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds, _account = _load_creds()
        if creds is None:
            return not_linked
        summary = str(args.get("summary") or "").strip()
        if not summary:
            return ToolOutcome(
                content="calendar_create_my_event requires a 'summary'.",
                is_error=True,
            )
        start = str(args.get("start") or "").strip()
        end = str(args.get("end") or "").strip()
        if not start or not end:
            return ToolOutcome(
                content=(
                    "calendar_create_my_event requires 'start' and 'end' "
                    "as ISO-8601 timestamps (e.g. 2026-04-20T14:00:00-07:00)."
                ),
                is_error=True,
            )
        attendees_raw = args.get("attendees") or []
        if not isinstance(attendees_raw, list):
            return ToolOutcome(
                content="'attendees' must be a list of email addresses.",
                is_error=True,
            )
        attendees = [str(a).strip() for a in attendees_raw if str(a).strip()]
        description = str(args.get("description") or "")
        location = str(args.get("location") or "")
        try:
            created = await asyncio.to_thread(
                _do_create_event,
                creds,
                summary=summary,
                start=start,
                end=end,
                attendees=attendees,
                description=description,
                location=location,
            )
        except Exception as e:
            log.exception("calendar_create_event_failed")
            return ToolOutcome(
                content=f"calendar_create_my_event failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"Created \"{created['summary']}\" "
                f"({created['start']} → {created['end']}). "
                f"Link: {created['html_link']}"
            ),
            data=created,
        )

    read_tool = Tool(
        name="calendar_read_my_today",
        description=(
            "List events on your primary Google Calendar for a given day "
            "(default: today). Returns start, end, summary, and attendees. "
            "Read-only; hitting the approval queue the first time lets you "
            "trust this for an hour if you want."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": (
                        "ISO date (YYYY-MM-DD). Omit for today in UTC."
                    ),
                },
            },
        },
        risk=RiskClass.NET_READ,
        handler=_read_today,
        account_binding=binding,
    )
    create_tool = Tool(
        name="calendar_create_my_event",
        description=(
            "Create an event on your primary Google Calendar. Always "
            "requires approval so you can review the summary, time, and "
            "attendees before it lands. Times must be ISO-8601 with a "
            "timezone offset."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title."},
                "start": {
                    "type": "string",
                    "description": "ISO-8601 start with offset, e.g. 2026-04-20T14:00:00-07:00.",
                },
                "end": {
                    "type": "string",
                    "description": "ISO-8601 end with offset.",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of attendee email addresses.",
                },
                "description": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["summary", "start", "end"],
        },
        risk=RiskClass.NET_WRITE,
        handler=_create_event,
        account_binding=binding,
    )
    return [read_tool, create_tool]


# ── synchronous Calendar helpers ────────────────────────────────────────


def _do_read_today(creds, date_str: str) -> list[dict]:
    service = creds.build("calendar", "v3")
    day = _parse_date(date_str) if date_str else datetime.now(UTC).date()
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    listing = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=MAX_EVENTS,
        )
        .execute()
    )
    events: list[dict] = []
    for e in listing.get("items", []) or []:
        events.append(
            {
                "id": e.get("id"),
                "summary": e.get("summary", "(no title)"),
                "start": _event_time(e.get("start")),
                "end": _event_time(e.get("end")),
                "attendees": [
                    a.get("email")
                    for a in (e.get("attendees") or [])
                    if a.get("email")
                ],
                "html_link": e.get("htmlLink", ""),
            }
        )
    return events


def _do_create_event(
    creds,
    *,
    summary: str,
    start: str,
    end: str,
    attendees: list[str],
    description: str,
    location: str,
) -> dict:
    service = creds.build("calendar", "v3")
    body: dict = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    created = (
        service.events()
        .insert(calendarId="primary", body=body, sendUpdates="all" if attendees else "none")
        .execute()
    )
    return {
        "id": created.get("id"),
        "summary": created.get("summary", summary),
        "start": _event_time(created.get("start")),
        "end": _event_time(created.get("end")),
        "html_link": created.get("htmlLink", ""),
        "attendees": [
            a.get("email")
            for a in (created.get("attendees") or [])
            if a.get("email")
        ],
    }


def _event_time(block) -> str:
    if not block:
        return ""
    return block.get("dateTime") or block.get("date") or ""


def _parse_date(s: str):
    from datetime import date

    return date.fromisoformat(s)
