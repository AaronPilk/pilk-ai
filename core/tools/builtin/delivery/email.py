"""``agent_email_deliver`` — a thin, opinionated wrapper around Gmail.

Why wrap Gmail at all? Agents send a lot of summaries; we want three
invariants that are awkward to enforce at the prompt layer:

1. **Consistent subject format.** Every delivery lands in the operator's
   inbox as ``[{agent_name}] {task_description}``. That lets a filter
   single-click-archive by agent.
2. **Always uses the "system" Google account.** Agents never send
   from the operator's personal inbox — it's a dedicated role account
   (``sentientpilkai@gmail.com`` in this deployment) whose entire job
   is outbound agent mail.
3. **Known-recipient bypass.** Mail to a small internal allowlist
   skips operator approval via a permanent TrustStore rule wired at
   daemon startup. Everything else queues for approval — fast when you
   want it, safe by default.

Attachments are supported: each path is read, sniffed for a MIME type,
and bundled as a ``MIMEApplication`` / ``MIMEImage`` / ``MIMEText``
part. Missing files surface a clean error (the agent is expected to
produce absolute paths to files it has already written).
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
from collections.abc import Callable, Iterable
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from core.identity import AccountsStore
from core.integrations.google.oauth import credentials_from_blob
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import AccountBinding, Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.agent_email_deliver")

# "system" role = the dedicated agent-outbound Google account. If the
# operator connects more than one Google account, the "system" role is
# the one that ``agent_email_deliver`` uses — never "user" (which is
# the operator's personal inbox).
_DELIVERY_ROLE = "system"

# Enforced at the tool layer. Subject is pre-formatted before it hits
# Gmail's send API; agents have no way to override.
SUBJECT_FORMAT = "[{agent_name}] {task_description}"


def make_agent_email_deliver_tool(accounts: AccountsStore) -> Tool:
    """Factory. Binds to ``AccountsStore`` so credentials resolve
    freshly on every call."""
    binding = AccountBinding(provider="google", role=_DELIVERY_ROLE)

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
            "No 'system' Google account connected — this is the outbound "
            "mail account every agent uses for deliveries. Open Settings "
            "→ Connected accounts and link a Google account with the "
            "'system' role and the gmail.send scope."
        ),
        is_error=True,
    )

    async def _deliver(args: dict, ctx: ToolContext) -> ToolOutcome:
        # ── Arg validation ─────────────────────────────────────
        to = args.get("to") or []
        if isinstance(to, str):
            # Agents sometimes fumble scalar-vs-list — be forgiving.
            to = [to]
        if not isinstance(to, list) or not to:
            return ToolOutcome(
                content="agent_email_deliver requires 'to' (non-empty list of emails).",
                is_error=True,
            )
        if any(not _looks_like_email(r) for r in to):
            return ToolOutcome(
                content=(
                    "agent_email_deliver: every 'to' entry must be a "
                    "valid-looking email address."
                ),
                is_error=True,
            )

        agent_name = str(args.get("agent_name") or ctx.agent_name or "").strip()
        if not agent_name:
            return ToolOutcome(
                content=(
                    "agent_email_deliver requires 'agent_name' (or a "
                    "ToolContext with agent_name set)."
                ),
                is_error=True,
            )
        task_description = str(args.get("task_description") or "").strip()
        if not task_description:
            return ToolOutcome(
                content="agent_email_deliver requires 'task_description'.",
                is_error=True,
            )
        body = str(args.get("body") or "")
        if not body.strip():
            return ToolOutcome(
                content="agent_email_deliver requires non-empty 'body'.",
                is_error=True,
            )

        attachments = args.get("attachments") or []
        if not isinstance(attachments, list):
            return ToolOutcome(
                content="'attachments' must be a list of absolute file paths.",
                is_error=True,
            )
        links = args.get("links") or []
        if not isinstance(links, list):
            return ToolOutcome(
                content="'links' must be a list of URL strings.",
                is_error=True,
            )

        # ── Resolve credential ─────────────────────────────────
        creds, account = _load_creds()
        if creds is None:
            return _not_linked

        # ── Build the MIME message ─────────────────────────────
        subject = SUBJECT_FORMAT.format(
            agent_name=agent_name, task_description=task_description
        )
        try:
            msg = _build_message(
                to=to,
                from_address=creds.email,
                subject=subject,
                body=body,
                attachments=attachments,
                links=links,
            )
        except _AttachmentError as e:
            return ToolOutcome(content=f"refused: {e}", is_error=True)

        # ── Send ────────────────────────────────────────────────
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        try:
            sent = await asyncio.to_thread(_do_send, creds, raw)
        except Exception as e:
            log.exception("agent_email_deliver_failed")
            return ToolOutcome(
                content=(
                    f"agent_email_deliver failed: {type(e).__name__}: {e}"
                ),
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"Sent to {', '.join(to)} (subject: {subject}). "
                f"Thread {sent.get('threadId', '')[:12]}…"
            ),
            data={
                "message_id": sent.get("id"),
                "thread_id": sent.get("threadId"),
                "to": list(to),
                "subject": subject,
                "from": account.email if account else None,
                "attachment_count": len(attachments),
                "link_count": len(links),
            },
        )

    return Tool(
        name="agent_email_deliver",
        description=(
            "Deliver an agent's work product via email. Subject is "
            "always formatted '[{agent_name}] {task_description}'; "
            "every delivery sends from the 'system' Google account "
            "(never the operator's personal inbox). Attachments are "
            "file paths the caller has already written. Links are "
            "appended to the body. RiskClass.NET_WRITE — queues for "
            "approval by default; internal recipients bypass via "
            "permanent trust rules seeded at startup."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Recipient email addresses.",
                },
                "agent_name": {
                    "type": "string",
                    "description": (
                        "Sender label for the subject. Defaults to the "
                        "ToolContext's agent_name when omitted."
                    ),
                },
                "task_description": {
                    "type": "string",
                    "description": "Short summary of what this delivery is about.",
                },
                "body": {"type": "string"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute filesystem paths to attach.",
                },
                "links": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "URLs appended to the body under a 'Links:' "
                        "header. Useful for deck/doc/page links without "
                        "sending an attachment."
                    ),
                },
            },
            "required": ["to", "task_description", "body"],
        },
        risk=RiskClass.NET_WRITE,
        handler=_deliver,
    )


# ── Helpers ───────────────────────────────────────────────────────


class _AttachmentError(Exception):
    pass


def _looks_like_email(s: object) -> bool:
    if not isinstance(s, str):
        return False
    stripped = s.strip()
    if not stripped or "@" not in stripped:
        return False
    # Reject whitespace inside — a common accidental-concatenation bug.
    return stripped == s and " " not in s


def _build_message(
    *,
    to: Iterable[str],
    from_address: str | None,
    subject: str,
    body: str,
    attachments: list[str],
    links: list[str],
) -> EmailMessage:
    msg = EmailMessage()
    msg["to"] = ", ".join(to)
    msg["subject"] = subject
    if from_address:
        msg["from"] = formataddr(("PILK", from_address))

    body_with_links = body
    if links:
        link_lines = "\n".join(f"- {link}" for link in links)
        body_with_links = f"{body.rstrip()}\n\nLinks:\n{link_lines}\n"
    msg.set_content(body_with_links)

    for raw_path in attachments:
        path = Path(os.path.expanduser(str(raw_path)))
        if not path.is_file():
            raise _AttachmentError(
                f"attachment not found: {path} (use an absolute path to an "
                "existing file)"
            )
        mime_type, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (
            (mime_type.split("/", 1)) if mime_type else ("application", "octet-stream")
        )
        data = path.read_bytes()
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    return msg


def _do_send(creds, raw_b64url: str) -> dict:
    service = creds.build("gmail", "v1")
    return (
        service.users()
        .messages()
        .send(userId="me", body={"raw": raw_b64url})
        .execute()
    )


# ── TrustStore predicate ──────────────────────────────────────────


def recipients_in_allowlist(
    allowed: Iterable[str],
) -> Callable[[dict], bool]:
    """Build a TrustStore args-predicate that returns True iff every
    address in ``args['to']`` is in ``allowed``.

    Separated from the tool handler so the daemon lifespan can
    construct permanent trust rules without owning the tool's internal
    argument shape. Strict subset check — if any recipient isn't on
    the allowlist, the predicate fails and the rule doesn't apply.
    """
    allow_set = {a.strip().lower() for a in allowed if a}

    def _predicate(args: dict) -> bool:
        to = args.get("to")
        if isinstance(to, str):
            to = [to]
        if not isinstance(to, list) or not to:
            return False
        return all(
            isinstance(r, str) and r.strip().lower() in allow_set for r in to
        )

    return _predicate


__all__ = [
    "SUBJECT_FORMAT",
    "make_agent_email_deliver_tool",
    "recipients_in_allowlist",
]
