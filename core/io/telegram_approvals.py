"""Telegram ↔ approvals bridge.

Every approval request that lands on the hub gets pushed to the
operator's Telegram chat as an inline-button card ("Approve" /
"Reject"). Tapping a button resolves the approval with the matching
decision and the card is rewritten in place to show the outcome, so
the operator's chat history is a faithful log of what's still pending.

### Shape

- ``TelegramApprovals`` subscribes to ``Hub`` and listens for two
  event types:
    - ``approval.created`` — send a card.
    - ``approval.resolved`` — edit the card to strip buttons + append
      a one-line decision marker.
- Callback-query updates come in via the existing ``TelegramBridge``
  long-poll loop (see ``allowed_updates=["message", "callback_query"]``).
  The bridge hands each callback_query to this class'
  ``handle_callback`` entry point, which parses ``callback_data`` and
  calls ``ApprovalManager.approve`` / ``reject``.

### Single-tenant guarantees

- Callbacks from any chat other than the configured ``chat_id`` are
  dropped with a short error toast. Telegram's bot-discovery surface
  is public enough that we treat every update as hostile by default.
- Callback data is a fixed two-token format: ``"<action>:<approval_id>"``.
  Anything else is ignored. This keeps the attack surface tiny — there
  is no way for a malformed button to resolve the wrong approval.
- Idempotent: tapping a button on an already-resolved card answers
  with "Already resolved." instead of 500-ing. Happens naturally if
  two devices race or the operator resolved the approval from the web
  dashboard first.

### What this doesn't do

- No multi-step trust-rule scope selector. The web dashboard still
  owns "approve + remember for 30 minutes"; the Telegram card is the
  one-tap fast path, and applies no trust rule.
- No reminder / escalation loop. If the operator ignores the card,
  the approval stays pending until resolved from anywhere.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from core.api.hub import Hub
from core.integrations.telegram import TelegramClient, TelegramError
from core.logging import get_logger
from core.policy.approvals import ApprovalManager

log = get_logger("pilkd.telegram.approvals")

# Telegram caps inline-button labels by pixel width, not char count, but
# 30 chars fits on a phone screen without wrapping.
MAX_BUTTON_LABEL_CHARS = 30
# Risk class markers that tell the operator at a glance what they're
# being asked to approve. Values match core.policy.risk.RiskClass.
_RISK_EMOJI = {
    "READ": "\U0001F440",           # 👀
    "WRITE_LOCAL": "\U0001F4DD",    # 📝
    "EXEC_LOCAL": "\U0001F527",     # 🔧
    "NET_READ": "\U0001F310",       # 🌐
    "BROWSE": "\U0001F310",         # 🌐
    "NET_WRITE": "\U0001F4E4",      # 📤
    "COMMS": "\U0001F4E7",          # 📧
    "FINANCIAL": "\U0001F4B0",      # 💰
    "IRREVERSIBLE": "\U000026A0\U0000FE0F",  # ⚠️
}


class TelegramApprovals:
    """Forwards the approval queue to the operator's Telegram chat.

    Construct one per daemon; call :meth:`start` in lifespan startup
    and :meth:`stop` in lifespan shutdown. The associated
    :class:`~core.io.telegram_bridge.TelegramBridge` must be given
    this instance's :meth:`handle_callback` so callback_query updates
    round-trip into approval decisions.
    """

    def __init__(
        self,
        *,
        client: TelegramClient,
        hub: Hub,
        approvals: ApprovalManager,
        chat_id: str,
    ) -> None:
        self._client = client
        self._hub = hub
        self._approvals = approvals
        self._chat_id = str(chat_id)
        # approval_id -> (chat_id, message_id, rendered_body). Populated
        # when we send the card and consulted when we rewrite it on
        # resolution. A hub restart wipes this; pending cards still
        # work via approve/reject but won't visibly update in chat.
        self._cards: dict[str, dict[str, Any]] = {}
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        self._hub.subscribe(self._on_event)
        self._started = True
        log.info("telegram_approvals_started", chat_id=self._chat_id)

    def stop(self) -> None:
        if not self._started:
            return
        self._hub.unsubscribe(self._on_event)
        self._started = False
        log.info("telegram_approvals_stopped")

    # ── hub event fan-in ─────────────────────────────────────────

    async def _on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "approval.created":
            await self._send_card(payload)
        elif event_type == "approval.resolved":
            await self._mark_resolved(payload)

    async def _send_card(self, payload: dict[str, Any]) -> None:
        approval_id = payload.get("id")
        if not isinstance(approval_id, str):
            return
        body = _format_request(payload)
        markup = {
            "inline_keyboard": [[
                {
                    "text": "✅ Approve",
                    "callback_data": f"approve:{approval_id}"[:64],
                },
                {
                    "text": "❌ Reject",
                    "callback_data": f"reject:{approval_id}"[:64],
                },
            ]],
        }
        try:
            sent = await self._client.send_message(
                body, reply_markup=markup,
            )
        except TelegramError as e:
            log.warning(
                "telegram_approval_card_send_failed",
                approval_id=approval_id,
                status=e.status,
                message=e.message,
            )
            return
        except Exception as e:
            log.warning(
                "telegram_approval_card_send_error",
                approval_id=approval_id,
                error=str(e),
            )
            return
        chat_id = str((sent.get("chat") or {}).get("id") or "")
        message_id = sent.get("message_id")
        if chat_id and isinstance(message_id, int):
            self._cards[approval_id] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "body": body,
            }

    async def _mark_resolved(self, payload: dict[str, Any]) -> None:
        approval_id = payload.get("id")
        if not isinstance(approval_id, str):
            return
        entry = self._cards.pop(approval_id, None)
        if entry is None:
            return
        decision = str(payload.get("decision") or "resolved")
        # Render the decision inline so the chat history shows the
        # final verdict without the operator scrolling to the approvals
        # tab. Strip the reply_markup by passing None explicitly —
        # Telegram interprets missing ``reply_markup`` as "keep the
        # existing one", so we pass an empty inline_keyboard instead.
        marker = _decision_marker(decision)
        new_body = f"{entry['body']}\n\n{marker}"
        with contextlib.suppress(TelegramError, Exception):
            await self._client.edit_message_text(
                chat_id=entry["chat_id"],
                message_id=entry["message_id"],
                text=new_body,
                reply_markup={"inline_keyboard": []},
            )

    # ── inbound callback_query dispatch ──────────────────────────

    async def handle_callback(self, update: dict[str, Any]) -> None:
        """Dispatch one ``callback_query`` update.

        Called by :class:`~core.io.telegram_bridge.TelegramBridge` for
        every update whose ``callback_query`` field is set. The bridge
        already owns the long-poll loop; this class owns the decode +
        route logic.
        """
        cbq = update.get("callback_query") or {}
        cbq_id = cbq.get("id")
        if not isinstance(cbq_id, str):
            return
        from_chat_id = str(
            ((cbq.get("message") or {}).get("chat") or {}).get("id") or ""
        )
        if from_chat_id and from_chat_id != self._chat_id:
            # Foreign chat — refuse without leaking whether the
            # approval_id exists.
            await self._safe_answer(cbq_id, text="Not allowed.")
            return
        data = cbq.get("data") or ""
        parts = str(data).split(":", 1)
        if len(parts) != 2:
            await self._safe_answer(cbq_id, text="Invalid button.")
            return
        action, approval_id = parts[0], parts[1]
        if not approval_id:
            await self._safe_answer(cbq_id, text="Invalid button.")
            return
        if action == "approve":
            try:
                await self._approvals.approve(
                    approval_id, reason="approved via Telegram",
                )
                await self._safe_answer(cbq_id, text="Approved.")
            except LookupError:
                await self._safe_answer(cbq_id, text="Already resolved.")
            except Exception as e:
                log.warning(
                    "telegram_approval_approve_failed",
                    approval_id=approval_id,
                    error=str(e),
                )
                await self._safe_answer(
                    cbq_id, text="Something went wrong.",
                )
        elif action == "reject":
            try:
                await self._approvals.reject(
                    approval_id, reason="rejected via Telegram",
                )
                await self._safe_answer(cbq_id, text="Rejected.")
            except LookupError:
                await self._safe_answer(cbq_id, text="Already resolved.")
            except Exception as e:
                log.warning(
                    "telegram_approval_reject_failed",
                    approval_id=approval_id,
                    error=str(e),
                )
                await self._safe_answer(
                    cbq_id, text="Something went wrong.",
                )
        else:
            await self._safe_answer(cbq_id, text="Unknown action.")

    async def _safe_answer(
        self, callback_query_id: str, *, text: str,
    ) -> None:
        # Telegram enforces a ~15s window on answerCallbackQuery; a
        # transient failure here is not worth crashing the bridge over.
        try:
            await self._client.answer_callback_query(
                callback_query_id, text=text,
            )
        except TelegramError as e:
            log.warning(
                "telegram_answer_callback_failed",
                status=e.status, message=e.message,
            )
        except Exception as e:
            log.warning(
                "telegram_answer_callback_error", error=str(e),
            )


# ── formatting helpers ──────────────────────────────────────────


def _format_request(payload: dict[str, Any]) -> str:
    """Render an approval request as a concise one-glance message.

    Two layers:

    1. If the tool is in ``_TOOL_SUMMARIES``, we lead with a one-line
       plain-English description of what will happen ("Create a GHL
       task…", "Send an email to…"). The raw args dump becomes a
       collapsed footer so a curious operator can still eyeball
       details, but the glanceable answer to "what is this approving?"
       is one sentence, not ten.
    2. For tools without a bespoke summary, we fall back to a cleaner
       arg dump (no ``repr()`` quote noise on short strings) so even
       the unknown tools read better than before.

    Uses plain text (no markdown) so we don't have to escape every
    stray asterisk or bracket that might appear in tool args.
    """
    risk = str(payload.get("risk_class") or "")
    emoji = _RISK_EMOJI.get(risk, "❓")
    tool = str(payload.get("tool_name") or "unknown_tool")
    agent = payload.get("agent_name") or "PILK"
    reason = payload.get("reason") or ""
    raw_args = payload.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}

    summary = _summarize_tool_call(tool, args)

    lines: list[str] = [f"{emoji} Approval requested"]
    if summary:
        lines.append("")
        lines.append(summary)
        lines.append("")
        lines.append(f"Agent: {agent}  ·  Tool: {tool}  ·  Risk: {risk}")
        # Only show "Reason" when it's actually informative — the
        # policy layer defaults it to "NET_WRITE: requires approval"
        # which just duplicates the risk class we already surfaced.
        if reason and not _is_redundant_reason(reason, risk):
            lines.append(f"Note: {reason}")
        return "\n".join(lines)

    # ── fallback path: no bespoke summary for this tool ────────────
    lines.append(f"{agent} wants to run {tool} ({risk})")
    if reason and not _is_redundant_reason(reason, risk):
        lines.append(f"Reason: {reason}")
    if args:
        lines.append("")
        lines.append("Args:")
        for k, v in args.items():
            lines.append(f"  {k}: {_short(v)}")
    return "\n".join(lines)


def _is_redundant_reason(reason: str, risk: str) -> bool:
    """True when the policy-layer ``reason`` is just echoing the risk
    class (``"NET_WRITE: requires approval"``) — we already show the
    risk class on its own line, so echoing it again is noise."""
    trimmed = reason.strip().lower()
    if not trimmed or not risk:
        return False
    boilerplate = f"{risk.lower()}: requires approval"
    return trimmed == boilerplate


def _short(value: Any, *, limit: int = 200) -> str:
    """Render a tool-arg value inline without flooding the card.

    Strings are rendered bare (no ``repr()`` quote noise), everything
    else falls back to ``repr()``. Long values get truncated with an
    ellipsis — the goal is "can the operator eyeball what's being
    run", not "round-trip the value".
    """
    # Collapse multi-line strings onto one row so one argument
    # doesn't explode the card. Real content stays on disk / in
    # the web dashboard if the operator wants full detail.
    s = value.replace("\n", " ↵ ") if isinstance(value, str) else repr(value)
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


# ── per-tool summary registry ─────────────────────────────────────
#
# Each entry takes the ``args`` dict and returns a one-line plain-
# English sentence. Return ``None`` if the entry can't produce a
# useful summary for this particular args shape and we should fall
# back to the raw-args path. ``_summarize_tool_call`` also returns
# ``None`` for unknown tools, same semantics.
#
# Guiding principles:
#   * One sentence, ~80-120 chars ideally.
#   * Lead with the verb: "Send", "Create", "Update", "Delete", "Run".
#   * Include the concrete thing being acted on (recipient, title,
#     path, amount) — the operator is approving *this specific* call.
#   * Quote strings with double quotes so they're visually distinct.
#   * Skip fields that are noisy (ids, internal references) unless
#     they're the only anchor we have.


def _q(s: Any) -> str:
    """Quote a string for inline display, truncating if huge."""
    text = str(s or "").strip().replace("\n", " ")
    if len(text) > 80:
        text = text[:79] + "…"
    return f'"{text}"'


def _opt(value: Any) -> str:
    """Empty string when value is falsy so a summary line can splice in
    optional fields without dangling "due None" / "to ''"."""
    return "" if value in (None, "", [], {}) else str(value)


def _fmt_ghl_task_create(a: dict[str, Any]) -> str:
    title = _q(a.get("title"))
    contact = _opt(a.get("contact_id"))
    due = _opt(a.get("due_date"))
    tail = []
    if contact:
        tail.append(f"for contact {contact}")
    if due:
        tail.append(f"due {due}")
    suffix = (" " + ", ".join(tail)) if tail else ""
    return f"Create a GHL task {title}{suffix}."


def _fmt_ghl_contact_mutation(verb: str) -> Callable[[dict[str, Any]], str]:
    def _fmt(a: dict[str, Any]) -> str:
        name = _opt(a.get("first_name")) + " " + _opt(a.get("last_name"))
        who = name.strip() or _opt(a.get("email")) or _opt(a.get("phone")) or _opt(a.get("contact_id")) or "a contact"
        return f"{verb} GHL contact {who}."
    return _fmt


def _fmt_ghl_send_email(a: dict[str, Any]) -> str:
    to = _opt(a.get("contact_id")) or _opt(a.get("to")) or "a contact"
    subject = _q(a.get("subject") or "(no subject)")
    return f"Send a GHL email to {to} with subject {subject}."


def _fmt_ghl_send_sms(a: dict[str, Any]) -> str:
    to = _opt(a.get("contact_id")) or _opt(a.get("to")) or "a contact"
    msg = _q(a.get("message"))
    return f"Send a GHL SMS to {to}: {msg}"


def _fmt_ghl_opportunity_create(a: dict[str, Any]) -> str:
    name = _q(a.get("name") or "an opportunity")
    pipeline = _opt(a.get("pipeline_id"))
    stage = _opt(a.get("pipeline_stage_id"))
    bits = [p for p in [pipeline and f"pipeline {pipeline}", stage and f"stage {stage}"] if p]
    tail = (" in " + ", ".join(bits)) if bits else ""
    return f"Create a GHL opportunity {name}{tail}."


def _fmt_ghl_opportunity_move_stage(a: dict[str, Any]) -> str:
    opp = _opt(a.get("opportunity_id")) or "an opportunity"
    stage = _opt(a.get("pipeline_stage_id")) or "a new stage"
    return f"Move GHL opportunity {opp} to stage {stage}."


def _fmt_ghl_workflow_add_contact(a: dict[str, Any]) -> str:
    contact = _opt(a.get("contact_id")) or "a contact"
    wf = _opt(a.get("workflow_id")) or "a workflow"
    return f"Add GHL contact {contact} to workflow {wf}."


def _fmt_ghl_appointment_create(a: dict[str, Any]) -> str:
    title = _q(a.get("title") or "an appointment")
    start = _opt(a.get("start_time"))
    tail = f" at {start}" if start else ""
    return f"Create a GHL appointment {title}{tail}."


def _fmt_gmail_send(a: dict[str, Any]) -> str:
    to = _opt(a.get("to")) or "a recipient"
    subject = _q(a.get("subject") or "(no subject)")
    return f"Send an email to {to} with subject {subject}."


def _fmt_gmail_draft(a: dict[str, Any]) -> str:
    to = _opt(a.get("to")) or "(no recipient)"
    subject = _q(a.get("subject") or "(no subject)")
    return f"Save a Gmail draft to {to} with subject {subject}."


def _fmt_calendar_create(a: dict[str, Any]) -> str:
    summary = _q(a.get("summary") or a.get("title") or "an event")
    start = _opt(a.get("start")) or _opt(a.get("start_time"))
    tail = f" starting {start}" if start else ""
    return f"Create a calendar event {summary}{tail}."


def _fmt_brain_note_write(a: dict[str, Any]) -> str:
    path = _opt(a.get("path")) or "a note"
    content = a.get("content") or ""
    size = len(content) if isinstance(content, str) else 0
    return f"Write brain note {path} ({size} bytes)."


def _fmt_fs_write(a: dict[str, Any]) -> str:
    path = _opt(a.get("path")) or "a file"
    content = a.get("content") or ""
    size = len(content) if isinstance(content, str) else 0
    return f"Write file {path} ({size} bytes)."


def _fmt_shell(a: dict[str, Any]) -> str:
    cmd = a.get("command") or a.get("cmd") or ""
    if isinstance(cmd, list):
        cmd = " ".join(str(p) for p in cmd)
    cmd = str(cmd)
    if len(cmd) > 120:
        cmd = cmd[:119] + "…"
    return f"Run shell: {cmd}"


def _fmt_finance_deposit(a: dict[str, Any]) -> str:
    amount = _opt(a.get("amount"))
    currency = _opt(a.get("currency"))
    return f"Deposit {amount} {currency}".rstrip() + "."


_TOOL_SUMMARIES: dict[str, Callable[[dict[str, Any]], str]] = {
    # GHL (CRM) writes — the ones that actually fire approval cards.
    "ghl_task_create": _fmt_ghl_task_create,
    "ghl_contact_create": _fmt_ghl_contact_mutation("Create"),
    "ghl_contact_update": _fmt_ghl_contact_mutation("Update"),
    "ghl_contact_delete": _fmt_ghl_contact_mutation("Delete"),
    "ghl_send_email": _fmt_ghl_send_email,
    "ghl_send_sms": _fmt_ghl_send_sms,
    "ghl_opportunity_create": _fmt_ghl_opportunity_create,
    "ghl_opportunity_update": _fmt_ghl_opportunity_create,
    "ghl_opportunity_move_stage": _fmt_ghl_opportunity_move_stage,
    "ghl_opportunity_delete": lambda a: f"Delete GHL opportunity {_opt(a.get('opportunity_id')) or '?'}.",
    "ghl_workflow_add_contact": _fmt_ghl_workflow_add_contact,
    "ghl_appointment_create": _fmt_ghl_appointment_create,
    "ghl_appointment_update": _fmt_ghl_appointment_create,
    # Gmail + Calendar writes.
    "gmail_send_as_pilk": _fmt_gmail_send,
    "gmail_send_as_me": _fmt_gmail_send,
    "gmail_draft_save_as_pilk": _fmt_gmail_draft,
    "gmail_draft_save_as_me": _fmt_gmail_draft,
    "calendar_create_my_event": _fmt_calendar_create,
    # Local writes.
    "brain_note_write": _fmt_brain_note_write,
    "brain_note_search_and_replace": lambda a: (
        f"Search-and-replace in brain note {_opt(a.get('path')) or '?'}."
    ),
    "fs_write": _fmt_fs_write,
    "computer_fs_write": _fmt_fs_write,
    "shell_exec": _fmt_shell,
    "computer_shell": _fmt_shell,
    # Financial.
    "finance_deposit": _fmt_finance_deposit,
}


def _summarize_tool_call(tool: str, args: dict[str, Any]) -> str | None:
    """Return a one-line human summary for ``(tool, args)`` or ``None``
    when we don't have a bespoke formatter. Any exception raised by a
    formatter is swallowed and falls back to the raw-args path so a
    single malformed args dict can't hide the approval card."""
    fn = _TOOL_SUMMARIES.get(tool)
    if fn is None:
        return None
    try:
        result = fn(args)
    except Exception as e:  # pragma: no cover - defense-in-depth
        log.warning(
            "telegram_approval_summary_failed",
            tool=tool, error=str(e),
        )
        return None
    return result if result else None


def _decision_marker(decision: str) -> str:
    if decision == "approved":
        return "✅ Approved via Telegram."
    if decision == "rejected":
        return "❌ Rejected via Telegram."
    if decision == "cancelled":
        return "\U0001F6AB Cancelled."
    return f"[{decision}]"


__all__ = [
    "MAX_BUTTON_LABEL_CHARS",
    "TelegramApprovals",
]
