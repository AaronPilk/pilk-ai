"""Sentinel → Telegram push bridge.

The supervisor already emits ``sentinel.incident`` events on the hub
for every new incident; this module subscribes to those events and
pushes a short alert to the operator's Telegram chat when the
severity hits HIGH or CRITICAL.

Design notes:

* One subscriber per daemon. Starts after the TelegramClient is
  constructed; calls ``client.send_message`` directly (push-only —
  no inline keyboard, no callback handling). Ack + trust flow lives
  on the dashboard's ``sentinel_acknowledge_incident`` button; the
  Telegram message is informational.
* Dedupe is already handled by the supervisor's per-incident dedupe
  window; we don't re-guard here.
* Severity floor is HIGH by default so low/med incidents stay in the
  jsonl log (they're too noisy for phone pings). Configurable at
  construction time to support a "debug mode" operator setup.
* Silent-fail on every Telegram error — the supervisor's external
  state must not depend on the messenger staying up.
"""

from __future__ import annotations

import contextlib
from typing import Any

from core.api.hub import Hub
from core.integrations.telegram import TelegramClient, TelegramError
from core.logging import get_logger
from core.sentinel.contracts import Severity

log = get_logger("pilkd.sentinel.telegram_alert")

# Severity floor: only incidents at or above this severity push to
# Telegram. HIGH + CRITICAL are "wake me up" events; med/low stay on
# the dashboard.
DEFAULT_MIN_SEVERITY = Severity.HIGH

# Severity → emoji prefix so the operator can scan a phone ping in
# < 1 second. Values map to the public severity enum.
_SEVERITY_MARKER = {
    Severity.LOW.value: "\U0001F7E2",      # 🟢
    Severity.MED.value: "\U0001F7E1",      # 🟡
    Severity.HIGH.value: "\U0001F7E0",     # 🟠
    Severity.CRITICAL.value: "\U0001F534",  # 🔴
}


class SentinelTelegramAlert:
    """Subscribe to ``sentinel.incident`` and push severity-gated
    alerts to the operator's Telegram chat."""

    def __init__(
        self,
        *,
        client: TelegramClient,
        hub: Hub,
        min_severity: Severity = DEFAULT_MIN_SEVERITY,
    ) -> None:
        self._client = client
        self._hub = hub
        self._min = min_severity
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._hub.subscribe(self._on_event)
        self._started = True
        log.info(
            "sentinel_telegram_alert_started",
            min_severity=self._min.value,
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._hub.unsubscribe(self._on_event)
        self._started = False
        log.info("sentinel_telegram_alert_stopped")

    async def _on_event(
        self, event_type: str, payload: dict[str, Any],
    ) -> None:
        if event_type != "sentinel.incident":
            return
        severity_str = str(payload.get("severity") or "").lower()
        try:
            severity = Severity(severity_str)
        except ValueError:
            log.warning(
                "sentinel_telegram_alert_unknown_severity",
                severity=severity_str,
            )
            return
        if severity.rank() < self._min.rank():
            return
        body = _format_incident(payload)
        # Fire-and-forget — a slow or failing Telegram call must not
        # block the hub's downstream listeners. ``with suppress`` is
        # belt-and-braces on top of TelegramClient's own error hoisting.
        with contextlib.suppress(TelegramError, Exception):
            try:
                await self._client.send_message(body)
            except TelegramError as e:
                log.warning(
                    "sentinel_telegram_alert_send_failed",
                    status=e.status,
                    message=e.message,
                )


def _format_incident(payload: dict[str, Any]) -> str:
    """Render one alert card.

    Layout:
        🔴 Sentinel — CRITICAL
        Agent: xauusd_execution_agent
        Kind: stale_heartbeat
        Reason: no heartbeat for 180s
        Remediation: restart_agent (ok)
        Recommended: check broker adapter + restart
        Incident: inc_abc123 · 2026-04-22T08:30:00Z
    """
    severity = str(payload.get("severity") or "").lower()
    marker = _SEVERITY_MARKER.get(severity, "\U000026A0\U0000FE0F")
    agent = payload.get("agent") or "unknown"
    kind = payload.get("kind") or "unknown"
    summary = payload.get("summary") or ""
    cause = payload.get("likely_cause") or ""
    action = payload.get("recommended_action") or ""
    remediation = payload.get("remediation")
    outcome = payload.get("outcome")
    incident_id = payload.get("id") or ""
    created = payload.get("created_at") or ""

    lines = [f"{marker} Sentinel — {severity.upper()}"]
    lines.append(f"Agent: {agent}")
    lines.append(f"Kind: {kind}")
    if summary:
        lines.append(f"Reason: {summary}")
    elif cause:
        lines.append(f"Reason: {cause}")
    if remediation:
        fmt_outcome = outcome or "pending"
        lines.append(f"Remediation: {remediation} ({fmt_outcome})")
    if action:
        lines.append(f"Recommended: {action}")
    if incident_id or created:
        lines.append(f"Incident: {incident_id} · {created}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MIN_SEVERITY",
    "SentinelTelegramAlert",
]
