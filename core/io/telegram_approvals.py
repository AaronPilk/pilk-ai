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

    Uses plain text (no markdown) so we don't have to escape every
    stray asterisk or bracket that might appear in tool args.
    """
    risk = str(payload.get("risk_class") or "")
    emoji = _RISK_EMOJI.get(risk, "❓")  # ❓
    tool = payload.get("tool_name") or "unknown_tool"
    agent = payload.get("agent_name") or "PILK"
    reason = payload.get("reason") or ""
    args = payload.get("args") or {}
    lines = [
        f"{emoji} Approval requested",
        f"{agent} wants to run {tool} ({risk})",
    ]
    if reason:
        lines.append(f"Reason: {reason}")
    # Pretty-print args one per line. Skip when there aren't any so we
    # don't dangle an empty header.
    if args:
        lines.append("")
        lines.append("Args:")
        for k, v in args.items():
            lines.append(f"  {k} = {_short(v)}")
    return "\n".join(lines)


def _short(value: Any, *, limit: int = 200) -> str:
    """Render a tool-arg value inline without flooding the card.

    Long strings get truncated with an ellipsis; nested structures
    are JSON-ish via repr() — the goal is "can the operator eyeball
    what's being run", not "round-trip the value".
    """
    s = repr(value)
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


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
