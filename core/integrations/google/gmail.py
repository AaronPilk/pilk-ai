"""Gmail tools backed by the authenticated Google account.

Four tools in this MVP:

- gmail_send         — compose + send an email (COMMS risk, always approval).
- gmail_search       — query the inbox with Gmail's native search syntax
                       (NET_READ risk; auto-trust friendly).
- gmail_read         — read a single message by id (NET_READ risk).
- gmail_thread_read  — read the last N messages in a thread in order
                       (NET_READ risk); used for triage follow-ups.

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

from core.identity import AccountsStore
from core.integrations.google.accounts import GoogleRole
from core.integrations.google.oauth import credentials_from_blob
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import AccountBinding, Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.gmail")

MAX_BODY_PREVIEW = 6000
MAX_THREAD_MESSAGES = 20
DEFAULT_THREAD_MESSAGES = 5
MAX_THREAD_BODY_CHARS = 1200


# Role-specific naming and copy. The planner sees tool names directly,
# so the suffixes (_as_pilk / _as_me, _pilk_inbox / _my_inbox) are
# meant to be read literally and picked by intent.
_ROLE_NAMES: dict[GoogleRole, dict[str, str]] = {
    "system": {
        "send": "gmail_send_as_pilk",
        "search": "gmail_search_pilk_inbox",
        "read": "gmail_read_pilk",
        "thread_read": "gmail_thread_read_pilk",
        "draft_save": "gmail_draft_save_as_pilk",
    },
    "user": {
        "send": "gmail_send_as_me",
        "search": "gmail_search_my_inbox",
        "read": "gmail_read_me",
        "thread_read": "gmail_thread_read_me",
        "draft_save": "gmail_draft_save_as_me",
    },
}

_ROLE_NOUN: dict[GoogleRole, str] = {
    "system": "PILK's operational Gmail",
    "user": "your working Gmail",
}

_ROLE_SENDER_PHRASE: dict[GoogleRole, str] = {
    "system": "PILK's own Gmail address",
    "user": "your real Gmail address",
}


def make_gmail_tools(role: GoogleRole, accounts: AccountsStore) -> list[Tool]:
    """Factory that produces the Gmail tool set bound to one role.

    Tool names are role-suffixed so the planner picks them by intent
    (e.g. "send from PILK" vs "send as me"). Handlers share logic; at
    call time each one resolves the current default account for
    (google, role) through AccountsStore and uses its OAuth tokens.

    Re-registering is not needed when the default changes — the binding
    is resolved freshly on every invocation.
    """
    names = _ROLE_NAMES[role]
    noun = _ROLE_NOUN[role]
    sender = _ROLE_SENDER_PHRASE[role]
    binding = AccountBinding(provider="google", role=role)

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
            f"{noun} isn't connected yet. Open Settings → Connected accounts "
            f"and link a {role} Google account."
        ),
        is_error=True,
    )

    async def _send(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds, _account = _load_creds()
        if creds is None:
            return _not_linked
        to = str(args.get("to") or "").strip()
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "")
        cc = str(args.get("cc") or "").strip()
        bcc = str(args.get("bcc") or "").strip()
        reply_to_thread_id = str(args.get("reply_to_thread_id") or "").strip()
        if not to or "@" not in to:
            return ToolOutcome(
                content=f"{names['send']} requires a valid 'to' address.",
                is_error=True,
            )
        if not subject:
            return ToolOutcome(
                content=f"{names['send']} requires a 'subject'.",
                is_error=True,
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
            sent = await asyncio.to_thread(
                _do_send, creds, raw, reply_to_thread_id or None,
            )
            thread_id = sent.get("threadId") or ""
            msg_id = sent.get("id") or ""
            suffix = (
                " (replied to existing thread)"
                if reply_to_thread_id else ""
            )
            return ToolOutcome(
                content=(
                    f"Sent to {to} (subject: {subject}){suffix}. "
                    f"Thread {thread_id[:12]}…"
                ),
                data={"thread_id": thread_id, "message_id": msg_id, "to": to},
            )
        except Exception as e:
            log.exception("gmail_send_failed", tool=names["send"])
            return ToolOutcome(
                content=f"{names['send']} failed: {type(e).__name__}: {e}",
                is_error=True,
            )

    async def _search(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds, _account = _load_creds()
        if creds is None:
            return _not_linked
        query = str(args.get("query") or "").strip()
        max_results = int(args.get("max_results") or 10)
        max_results = max(1, min(max_results, 25))
        try:
            results = await asyncio.to_thread(
                _do_search, creds, query, max_results
            )
        except Exception as e:
            log.exception("gmail_search_failed", tool=names["search"])
            return ToolOutcome(
                content=f"{names['search']} failed: {type(e).__name__}: {e}",
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
        creds, _account = _load_creds()
        if creds is None:
            return _not_linked
        message_id = str(args.get("message_id") or "").strip()
        if not message_id:
            return ToolOutcome(
                content=f"{names['read']} requires a 'message_id'.",
                is_error=True,
            )
        try:
            msg = await asyncio.to_thread(_do_read, creds, message_id)
        except Exception as e:
            log.exception("gmail_read_failed", tool=names["read"])
            return ToolOutcome(
                content=f"{names['read']} failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"From: {msg['from']}\nTo: {msg['to']}\nSubject: {msg['subject']}\n"
                f"Date: {msg['date']}\n\n{msg['body'][:MAX_BODY_PREVIEW]}"
            ),
            data=msg,
        )

    async def _draft_save(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds, _account = _load_creds()
        if creds is None:
            return _not_linked
        to = str(args.get("to") or "").strip()
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "")
        cc = str(args.get("cc") or "").strip()
        bcc = str(args.get("bcc") or "").strip()
        reply_to_thread_id = str(args.get("reply_to_thread_id") or "").strip()
        if not to or "@" not in to:
            return ToolOutcome(
                content=f"{names['draft_save']} requires a valid 'to' address.",
                is_error=True,
            )
        if not subject:
            return ToolOutcome(
                content=f"{names['draft_save']} requires a 'subject'.",
                is_error=True,
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
            draft = await asyncio.to_thread(
                _do_draft_save, creds, raw, reply_to_thread_id or None,
            )
            draft_id = draft.get("id") or ""
            msg_obj = draft.get("message") or {}
            thread_id = msg_obj.get("threadId") or ""
            return ToolOutcome(
                content=(
                    f"Draft saved to {to} (subject: {subject}). "
                    f"Draft {draft_id[:12]}… — review in Gmail drafts, "
                    "then send when ready."
                ),
                data={
                    "draft_id": draft_id,
                    "thread_id": thread_id,
                    "to": to,
                    "subject": subject,
                },
            )
        except Exception as e:
            log.exception("gmail_draft_save_failed", tool=names["draft_save"])
            return ToolOutcome(
                content=f"{names['draft_save']} failed: {type(e).__name__}: {e}",
                is_error=True,
            )

    async def _thread_read(args: dict, ctx: ToolContext) -> ToolOutcome:
        creds, _account = _load_creds()
        if creds is None:
            return _not_linked
        thread_id = str(args.get("thread_id") or "").strip()
        if not thread_id:
            return ToolOutcome(
                content=f"{names['thread_read']} requires a 'thread_id'.",
                is_error=True,
            )
        raw_max = args.get("max_messages") or DEFAULT_THREAD_MESSAGES
        try:
            max_messages = int(raw_max)
        except (TypeError, ValueError):
            max_messages = DEFAULT_THREAD_MESSAGES
        max_messages = max(1, min(max_messages, MAX_THREAD_MESSAGES))
        try:
            thread = await asyncio.to_thread(
                _do_thread_read, creds, thread_id, max_messages
            )
        except Exception as e:
            log.exception("gmail_thread_read_failed", tool=names["thread_read"])
            return ToolOutcome(
                content=f"{names['thread_read']} failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        sections = [
            (
                f"[{i + 1}/{thread['returned']}]  {m['from']}  ·  {m['date']}\n"
                f"{m['body']}"
            )
            for i, m in enumerate(thread["messages"])
        ]
        header = (
            f"Thread: {thread['subject']}  "
            f"({thread['returned']} of {thread['total']} messages)\n\n"
        )
        return ToolOutcome(content=header + "\n\n---\n\n".join(sections), data=thread)

    send_tool = Tool(
        name=names["send"],
        description=(
            f"Compose and send an email from {sender}. COMMS risk — always "
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
                "reply_to_thread_id": {
                    "type": "string",
                    "description": (
                        "Optional Gmail threadId to attach this message "
                        "to. When set, the message lands inside the "
                        "existing thread instead of starting a new one — "
                        "Gmail threads it on both ends. Get the thread_id "
                        "from a prior gmail_search / gmail_thread_read "
                        "result."
                    ),
                },
            },
            "required": ["to", "subject", "body"],
        },
        risk=RiskClass.COMMS,
        handler=_send,
        account_binding=binding,
    )
    search_tool = Tool(
        name=names["search"],
        description=(
            f"Search {noun} using Gmail's native query syntax "
            "(e.g. 'from:example.com after:2026/04/01 is:unread'). Returns "
            f"up to 25 messages with id, from, subject. No body — use "
            f"{names['read']} to pull a full message."
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
        account_binding=binding,
    )
    read_tool = Tool(
        name=names["read"],
        description=(
            f"Fetch the full body of a single message from {noun} by id. "
            "Returns From/To/Subject/Date and up to ~6 KB of body text."
        ),
        input_schema={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        risk=RiskClass.NET_READ,
        handler=_read,
        account_binding=binding,
    )
    thread_read_tool = Tool(
        name=names["thread_read"],
        description=(
            f"Read the last N messages in a thread on {noun} in order, with "
            "sender, date, and body (each trimmed to ~1.2 KB). Use when "
            f"{names['read']} has surfaced a message that needs prior "
            "context, or when triaging a back-and-forth thread. Up to 20 "
            "messages per call."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "max_messages": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_THREAD_MESSAGES,
                },
            },
            "required": ["thread_id"],
        },
        risk=RiskClass.NET_READ,
        handler=_thread_read,
        account_binding=binding,
    )
    draft_save_tool = Tool(
        name=names["draft_save"],
        description=(
            f"Save a Gmail draft from {sender} without sending. Use "
            "when the operator wants to review a message before it "
            "goes out, or when composing a reply they'll edit later. "
            "Returns the draft_id — the draft appears in Gmail's "
            "Drafts folder and can be sent from there. WRITE_LOCAL "
            "risk, not COMMS: nothing leaves the mailbox."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text body."},
                "cc": {"type": "string", "description": "Optional CC (comma-separated)."},
                "bcc": {"type": "string", "description": "Optional BCC."},
                "reply_to_thread_id": {
                    "type": "string",
                    "description": (
                        "Optional Gmail threadId. When set, the draft "
                        "attaches to the existing thread so the reply "
                        "threads correctly if the operator sends it."
                    ),
                },
            },
            "required": ["to", "subject", "body"],
        },
        # Nothing actually goes out, so we treat this the same as any
        # other local write. The operator reviews the draft in Gmail
        # and hits send themselves (or asks PILK to).
        risk=RiskClass.WRITE_LOCAL,
        handler=_draft_save,
        account_binding=binding,
    )
    return [send_tool, search_tool, read_tool, thread_read_tool, draft_save_tool]


# ── synchronous Google API helpers (run in a thread) ───────────────────


def _do_send(
    creds,
    raw_b64url: str,
    reply_to_thread_id: str | None = None,
) -> dict:
    service = creds.build("gmail", "v1")
    body: dict = {"raw": raw_b64url}
    if reply_to_thread_id:
        # Gmail threads by attaching messages to an existing threadId.
        # Subject + References headers would also normally matter for
        # threading on OTHER clients, but the Gmail web UI threads on
        # threadId alone; we keep the body simple and let the operator
        # pass a threading-friendly subject themselves.
        body["threadId"] = reply_to_thread_id
    return (
        service.users()
        .messages()
        .send(userId="me", body=body)
        .execute()
    )


def _do_draft_save(
    creds,
    raw_b64url: str,
    reply_to_thread_id: str | None = None,
) -> dict:
    service = creds.build("gmail", "v1")
    message: dict = {"raw": raw_b64url}
    if reply_to_thread_id:
        message["threadId"] = reply_to_thread_id
    body: dict = {"message": message}
    return (
        service.users()
        .drafts()
        .create(userId="me", body=body)
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


def _do_thread_read(creds, thread_id: str, max_messages: int) -> dict:
    service = creds.build("gmail", "v1")
    thread = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    raw_messages = thread.get("messages") or []
    total = len(raw_messages)
    # Take the most recent `max_messages`, keep chronological order.
    window = raw_messages[-max_messages:]
    subject = ""
    parsed: list[dict] = []
    for m in window:
        payload = m.get("payload") or {}
        headers = {
            h["name"]: h.get("value", "") for h in (payload.get("headers") or [])
        }
        if not subject:
            subject = headers.get("Subject", "(no subject)")
        body = _extract_plain_body(payload)
        if len(body) > MAX_THREAD_BODY_CHARS:
            body = body[:MAX_THREAD_BODY_CHARS].rstrip() + "…"
        parsed.append(
            {
                "id": m.get("id"),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", "(no subject)"),
                "date": headers.get("Date", ""),
                "body": body,
            }
        )
    return {
        "thread_id": thread_id,
        "subject": subject or "(no subject)",
        "total": total,
        "returned": len(parsed),
        "messages": parsed,
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
