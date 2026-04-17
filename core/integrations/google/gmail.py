"""Gmail tools backed by the authenticated Google account.

Three tools in this MVP:

- gmail_send    — compose + send an email (COMMS risk, always approval).
- gmail_search  — query the inbox with Gmail's native search syntax
                  (NET_READ risk; auto-trust friendly).
- gmail_read    — read a single message by id (NET_READ risk).

The SDK call surface is synchronous; we run each blocking call through
`asyncio.to_thread` so we never block the event loop. Error bodies
are surfaced verbatim to the caller so failed sends are visibly logged
in the plan step, not swallowed.

Risk classes were picked to make sending always land in the approval
queue — "PILK wants to email X: <subject>" is a prompt you want to
see, not a trust-rule shortcut.
"""

from __future__ import annotations

import asyncio
import base64
from email.mime.text import MIMEText
from pathlib import Path

from core.integrations.google.oauth import load_credentials
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.gmail")

MAX_BODY_PREVIEW = 6000


def make_gmail_tools(credentials_path: Path) -> list[Tool]:
    """Factory that produces the Gmail tool set bound to a credentials file."""

    async def _send(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds = load_credentials(credentials_path)
        if creds is None:
            return ToolOutcome(
                content="Gmail isn't linked yet. Run `python -m scripts.link_google`.",
                is_error=True,
            )
        to = str(args.get("to") or "").strip()
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "")
        cc = str(args.get("cc") or "").strip()
        bcc = str(args.get("bcc") or "").strip()
        if not to or "@" not in to:
            return ToolOutcome(
                content="gmail_send requires a valid 'to' address.", is_error=True
            )
        if not subject:
            return ToolOutcome(
                content="gmail_send requires a 'subject'.", is_error=True
            )
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["to"] = to
            msg["subject"] = subject
            if cc:
                msg["cc"] = cc
            if bcc:
                msg["bcc"] = bcc
            if creds.email:
                msg["from"] = creds.email
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
            sent = await asyncio.to_thread(_do_send, creds, raw)
            thread_id = sent.get("threadId") or ""
            msg_id = sent.get("id") or ""
            return ToolOutcome(
                content=(
                    f"Sent to {to} (subject: {subject}). "
                    f"Thread {thread_id[:12]}…"
                ),
                data={"thread_id": thread_id, "message_id": msg_id, "to": to},
            )
        except Exception as e:
            log.exception("gmail_send_failed")
            return ToolOutcome(
                content=f"gmail_send failed: {type(e).__name__}: {e}", is_error=True
            )

    async def _search(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds = load_credentials(credentials_path)
        if creds is None:
            return ToolOutcome(
                content="Gmail isn't linked yet. Run `python -m scripts.link_google`.",
                is_error=True,
            )
        query = str(args.get("query") or "").strip()
        max_results = int(args.get("max_results") or 10)
        max_results = max(1, min(max_results, 25))
        try:
            results = await asyncio.to_thread(
                _do_search, creds, query, max_results
            )
        except Exception as e:
            log.exception("gmail_search_failed")
            return ToolOutcome(
                content=f"gmail_search failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        lines = [
            f"{r['from'][:60]:<60} · {r['subject'][:80]}  [{r['id']}]"
            for r in results
        ]
        header = f"{len(results)} message(s) for query {query!r}:\n\n"
        return ToolOutcome(
            content=header + "\n".join(lines) if lines else f"No messages match {query!r}.",
            data={"query": query, "results": results},
        )

    async def _read(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds = load_credentials(credentials_path)
        if creds is None:
            return ToolOutcome(
                content="Gmail isn't linked yet. Run `python -m scripts.link_google`.",
                is_error=True,
            )
        message_id = str(args.get("message_id") or "").strip()
        if not message_id:
            return ToolOutcome(
                content="gmail_read requires a 'message_id'.", is_error=True
            )
        try:
            msg = await asyncio.to_thread(_do_read, creds, message_id)
        except Exception as e:
            log.exception("gmail_read_failed")
            return ToolOutcome(
                content=f"gmail_read failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"From: {msg['from']}\nTo: {msg['to']}\nSubject: {msg['subject']}\n"
                f"Date: {msg['date']}\n\n{msg['body'][:MAX_BODY_PREVIEW]}"
            ),
            data=msg,
        )

    send_tool = Tool(
        name="gmail_send",
        description=(
            "Send an email from PILK's Gmail account. COMMS risk — always "
            "flows through the approval queue so you can review the "
            "recipient, subject, and body before it goes out."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text body."},
                "cc": {"type": "string", "description": "Optional CC (comma-separated)."},
                "bcc": {"type": "string", "description": "Optional BCC."},
            },
            "required": ["to", "subject", "body"],
        },
        risk=RiskClass.COMMS,
        handler=_send,
    )
    search_tool = Tool(
        name="gmail_search",
        description=(
            "Search PILK's Gmail inbox using Gmail's native query syntax "
            "(e.g. 'from:example.com after:2026/04/01 is:unread'). Returns "
            "up to 25 messages with id, from, subject. No body — use "
            "gmail_read to pull a full message."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            "required": ["query"],
        },
        risk=RiskClass.NET_READ,
        handler=_search,
    )
    read_tool = Tool(
        name="gmail_read",
        description=(
            "Fetch the full body of a single Gmail message by id. "
            "Returns From/To/Subject/Date and up to ~6 KB of body text."
        ),
        input_schema={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        risk=RiskClass.NET_READ,
        handler=_read,
    )
    return [send_tool, search_tool, read_tool]


# ── synchronous Google API helpers (run in a thread) ───────────────────


def _do_send(creds, raw_b64url: str) -> dict:
    service = creds.build("gmail", "v1")
    return (
        service.users()
        .messages()
        .send(userId="me", body={"raw": raw_b64url})
        .execute()
    )


def _do_search(creds, query: str, max_results: int) -> list[dict]:
    service = creds.build("gmail", "v1")
    listing = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    ids = [m["id"] for m in (listing.get("messages") or [])]
    out: list[dict] = []
    for mid in ids:
        try:
            m = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
                .execute()
            )
            headers = {
                h["name"]: h.get("value", "")
                for h in (m.get("payload") or {}).get("headers") or []
            }
            out.append(
                {
                    "id": mid,
                    "snippet": m.get("snippet") or "",
                    "subject": headers.get("Subject", "(no subject)"),
                    "from": headers.get("From", ""),
                    "date": headers.get("Date", ""),
                }
            )
        except Exception:  # pragma: no cover
            continue
    return out


def _do_read(creds, message_id: str) -> dict:
    service = creds.build("gmail", "v1")
    m = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    payload = m.get("payload") or {}
    headers = {
        h["name"]: h.get("value", "") for h in (payload.get("headers") or [])
    }
    body = _extract_plain_body(payload)
    return {
        "id": m.get("id"),
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "subject": headers.get("Subject", "(no subject)"),
        "date": headers.get("Date", ""),
        "snippet": m.get("snippet") or "",
        "body": body,
    }


def _extract_plain_body(payload: dict) -> str:
    """Prefer text/plain; fall back to text/html stripped, else snippet."""
    if payload.get("mimeType") == "text/plain":
        return _decode_body(payload.get("body") or {})
    for part in payload.get("parts") or []:
        if part.get("mimeType") == "text/plain":
            return _decode_body(part.get("body") or {})
        # Nested multipart
        if part.get("parts"):
            nested = _extract_plain_body(part)
            if nested:
                return nested
    # Fallback — any text/html, crudely stripped
    for part in payload.get("parts") or []:
        if part.get("mimeType") == "text/html":
            return _strip_html(_decode_body(part.get("body") or {}))
    return ""


def _decode_body(body: dict) -> str:
    data = body.get("data") or ""
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", "replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    # Quick and dirty — good enough for email plaintext fallback.
    import re

    no_tags = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", no_tags).strip()
