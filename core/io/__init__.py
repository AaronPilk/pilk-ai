"""I/O surfaces that bridge external inputs into the orchestrator.

The web dashboard talks to pilkd over WebSocket + REST; this package
covers the other ways the operator gets messages in:

- ``telegram_bridge`` — long-polls the Telegram Bot API and feeds
  inbound messages into the orchestrator's free-chat path.

Future additions (SMS/iMessage/Matrix) live here too.
"""

from __future__ import annotations

from core.io.telegram_bridge import TelegramBridge

__all__ = ["TelegramBridge"]
