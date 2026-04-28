"""Proactive alerts foundation.

Three concerns, kept small and explicit:

  - ``AlertSettings`` — operator-tunable knobs (max-per-day, min
    score, digest-only mode, Telegram enable, scheduled briefs).
    Stored as key/value rows in ``alert_settings_kv`` with safe
    defaults baked in. Telegram + scheduled briefs default OFF.
  - ``AlertStore`` — append-only event log in ``alerts``. Carries
    delivery decision so the dashboard can replay history.
  - ``AlertRouter`` — given a candidate ``AlertCandidate``, decides
    a delivery channel (silent / digest / dashboard / telegram)
    based on settings + per-topic overrides + dedupe + daily cap,
    then writes a row through the store.

The router NEVER fires Telegram unless ``telegram_enabled=true``
AND quiet hours allow it AND the topic isn't muted AND we're
under the daily cap. Same defensive posture for any future
channel.
"""

from core.alerts.router import AlertCandidate, AlertRouter, RoutingDecision
from core.alerts.settings import AlertSettings
from core.alerts.store import AlertEvent, AlertStore

__all__ = [
    "AlertCandidate",
    "AlertEvent",
    "AlertRouter",
    "AlertSettings",
    "AlertStore",
    "RoutingDecision",
]
