"""Alert router — decides where (if anywhere) an alert candidate
goes. Defensive by default: when in doubt, drop to ``digest`` (the
silent / non-pushy channel).

Decision precedence:

  1. ``mute`` topic override → silent
  2. ``min_score`` floor not met → silent
  3. Active mute window on the topic → silent
  4. Already seen (dedupe within 24h) → silent
  5. ``digest_only=True`` (the default) → digest
  6. ``mode=push`` topic override:
       - daily push cap reached → digest
       - quiet hours active → digest
       - telegram_enabled=False → digest
       - else → telegram
  7. Default mode (no per-topic override): digest

Every decision becomes one row in ``alerts`` so the operator can
see exactly what happened. The router never throws on edge cases
that shouldn't fire — it picks the safer outcome.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from core.alerts.settings import (
    AlertSettings,
    AlertSettingsSnapshot,
    TopicOverride,
    TopicOverrideStore,
)
from core.alerts.store import AlertEvent, AlertStore
from core.logging import get_logger

log = get_logger("pilkd.alerts.router")


@dataclass
class AlertCandidate:
    """Caller-supplied input. The router synthesises a ``dedupe_key``
    from ``kind + dedupe_seed`` if the caller doesn't pass one in,
    so call sites don't have to think about it."""

    kind: str
    title: str
    body: str | None = None
    severity: str = "info"
    project_slug: str | None = None
    topic_slug: str | None = None
    source_slug: str | None = None
    score: int | None = None
    dedupe_seed: str | None = None  # falls back to title
    dedupe_key: str | None = None   # explicit override
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutingDecision:
    delivery: str   # silent|digest|dashboard|telegram
    reason: str
    suppressed_by: str | None = None
    event: AlertEvent | None = None


class AlertRouter:
    def __init__(
        self,
        *,
        store: AlertStore,
        settings: AlertSettings,
        topic_overrides: TopicOverrideStore,
        global_quiet_hours_local: str = "off",
        global_quiet_hours_tz: str = "UTC",
    ) -> None:
        self._store = store
        self._settings = settings
        self._overrides = topic_overrides
        self._global_qh = global_quiet_hours_local
        self._global_tz = global_quiet_hours_tz

    async def route(self, c: AlertCandidate) -> RoutingDecision:
        snap = await self._settings.get()
        dedupe_key = c.dedupe_key or self._derive_dedupe_key(c)
        if await self._store.already_seen(dedupe_key):
            return await self._record(
                c, dedupe_key,
                delivery="silent",
                reason="duplicate_within_24h",
            )
        # Floor on relevance — applies to ALL channels including
        # digest. Below-floor items are dropped silently to keep
        # the digest readable.
        if (
            c.score is not None
            and snap.min_score is not None
            and c.score < snap.min_score
        ):
            return await self._record(
                c, dedupe_key,
                delivery="silent",
                reason="below_min_score",
            )
        override: TopicOverride | None = None
        if c.topic_slug:
            override = await self._overrides.get(c.topic_slug)
        if override is not None:
            if override.mode == "mute":
                return await self._record(
                    c, dedupe_key,
                    delivery="silent",
                    reason="topic_muted",
                )
            if override.mute_until and self._mute_active(override.mute_until):
                return await self._record(
                    c, dedupe_key,
                    delivery="silent",
                    reason="topic_mute_window_active",
                )
        # Digest-only mode: skip every push channel and route to digest.
        if snap.digest_only:
            return await self._record(
                c, dedupe_key,
                delivery="digest",
                reason="digest_only_mode",
            )
        # Push path is only opened by an explicit per-topic override.
        if override is not None and override.mode == "push":
            if not snap.telegram_enabled:
                return await self._record(
                    c, dedupe_key,
                    delivery="digest",
                    reason="telegram_not_enabled",
                )
            if self._quiet_hours_active(snap):
                return await self._record(
                    c, dedupe_key,
                    delivery="digest",
                    reason="quiet_hours",
                )
            pushed_today = await self._store.count_pushes_today()
            if pushed_today >= snap.max_per_day:
                return await self._record(
                    c, dedupe_key,
                    delivery="digest",
                    reason="max_per_day_cap",
                )
            return await self._record(
                c, dedupe_key,
                delivery="telegram",
                reason="push_topic_override",
            )
        return await self._record(
            c, dedupe_key,
            delivery="digest",
            reason="default_route",
        )

    async def _record(
        self,
        c: AlertCandidate,
        dedupe_key: str,
        *,
        delivery: str,
        reason: str,
    ) -> RoutingDecision:
        event = await self._store.insert(
            kind=c.kind,
            title=c.title,
            body=c.body,
            severity=c.severity,
            project_slug=c.project_slug,
            topic_slug=c.topic_slug,
            source_slug=c.source_slug,
            score=c.score,
            dedupe_key=dedupe_key,
            delivery=delivery,
            metadata={**c.metadata, "routing_reason": reason},
        )
        log.info(
            "alert_routed",
            kind=c.kind,
            delivery=delivery,
            reason=reason,
            dedupe_key=dedupe_key,
            topic=c.topic_slug,
            score=c.score,
        )
        return RoutingDecision(
            delivery=delivery,
            reason=reason,
            event=event,
        )

    @staticmethod
    def _derive_dedupe_key(c: AlertCandidate) -> str:
        seed = (
            c.dedupe_seed
            or f"{c.kind}|{c.topic_slug or ''}|{c.source_slug or ''}|{c.title}"
        )
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]

    def _mute_active(self, mute_until: str) -> bool:
        try:
            t = datetime.fromisoformat(mute_until)
        except ValueError:
            return False
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        return datetime.now(UTC) < t

    def _quiet_hours_active(
        self, snap: AlertSettingsSnapshot,
    ) -> bool:
        window = (snap.quiet_hours or self._global_qh or "").strip()
        if not window or window.lower() == "off":
            return False
        try:
            start_s, end_s = window.split("-", 1)
            start = _parse_hhmm(start_s.strip())
            end = _parse_hhmm(end_s.strip())
        except (ValueError, AttributeError):
            return False
        try:
            tz = ZoneInfo(self._global_tz or "UTC")
        except Exception:
            tz = ZoneInfo("UTC")
        now_local = datetime.now(tz).time()
        if start <= end:
            return start <= now_local < end
        # Window wraps past midnight (e.g. 22:00-08:00).
        return now_local >= start or now_local < end


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(hour=int(h), minute=int(m))


__all__ = ["AlertCandidate", "AlertRouter", "RoutingDecision"]
