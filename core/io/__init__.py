"""I/O surfaces that bridge external inputs into the orchestrator.

The web dashboard talks to pilkd over WebSocket + REST; this package
covers the other ways the operator gets messages in:

- ``telegram_bridge`` — long-polls the Telegram Bot API and feeds
  inbound messages into the orchestrator's free-chat path.
- ``telegram_approvals`` — fans approval-queue events out to the
  operator's Telegram chat with Approve / Reject buttons, and
  round-trips button taps back into ``ApprovalManager`` decisions.

Future additions (SMS/iMessage/Matrix) live here too.
"""

from __future__ import annotations

from core.io.telegram_approvals import TelegramApprovals
from core.io.telegram_bridge import TelegramBridge

__all__ = ["TelegramApprovals", "TelegramBridge"]
