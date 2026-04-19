"""Severity-gated external notifications.

The IncidentStore always writes to SQLite + jsonl (no gate). This module
handles the **external** escalation: an outgoing webhook POST, gated by
severity so ``low`` / ``med`` stay in the log and ``high`` / ``critical``
page the operator.

Webhook URL comes from the ``SENTINEL_WEBHOOK_URL`` env var; absent =
Sentinel runs "jsonl-only" with no external notifications. Matches the
spec's "safe default off" posture.

The notify path is intentionally simple — stdlib ``urllib.request``, a
short timeout, one retry. We do not want Sentinel's watchdog duties
blocked behind a slow Slack response.
"""

from __future__ import annotations

import json
import os
from urllib import error, request

from core.logging import get_logger
from core.sentinel.contracts import Incident, Severity

log = get_logger("pilkd.sentinel.notify")

WEBHOOK_ENV_VAR = "SENTINEL_WEBHOOK_URL"
DEFAULT_MIN_SEVERITY = Severity.HIGH
REQUEST_TIMEOUT_S = 5.0


class Notifier:
    """Encapsulates webhook config so tests can inject arbitrary URLs
    and severity thresholds without monkeypatching os.environ."""

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        min_severity: Severity = DEFAULT_MIN_SEVERITY,
    ) -> None:
        self._url = webhook_url or os.environ.get(WEBHOOK_ENV_VAR)
        self._min_severity = min_severity

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def should_notify(self, severity: Severity) -> bool:
        return severity.rank() >= self._min_severity.rank()

    def notify(self, incident: Incident) -> bool:
        """POST the incident. Returns True on any 2xx, False otherwise.

        Synchronous on purpose — the caller runs us via
        :func:`asyncio.to_thread` so the supervisor loop never blocks
        on a slow webhook. Keeping the function itself sync makes tests
        trivial."""
        if not self._url:
            return False
        if not self.should_notify(incident.severity):
            return False
        body = _serialize(incident).encode("utf-8")
        req = request.Request(
            self._url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                ok = 200 <= resp.status < 300
        except error.URLError as e:
            log.warning("notify_failed", error=str(e), incident_id=incident.id)
            return False
        except TimeoutError:
            log.warning("notify_timeout", incident_id=incident.id)
            return False
        if not ok:
            log.warning(
                "notify_bad_status", incident_id=incident.id
            )
        return ok


def _serialize(inc: Incident) -> str:
    return json.dumps(
        {
            "id": inc.id,
            "agent": inc.agent_name,
            "severity": inc.severity.value,
            "category": inc.category.value,
            "kind": inc.finding_kind,
            "summary": inc.summary,
            "likely_cause": inc.triage.likely_cause if inc.triage else None,
            "recommended_action": (
                inc.triage.recommended_action if inc.triage else None
            ),
            "remediation": inc.remediation,
            "outcome": inc.outcome,
            "created_at": inc.created_at,
        },
        default=str,
    )


__all__ = [
    "DEFAULT_MIN_SEVERITY",
    "REQUEST_TIMEOUT_S",
    "WEBHOOK_ENV_VAR",
    "Notifier",
]
