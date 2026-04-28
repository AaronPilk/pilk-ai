"""Sentinel rules for the Intelligence Engine.

Currently exports one rule:

  ``intel_source_health`` — fires a Sentinel ``Finding`` when an
  ``intel_sources`` row has ``consecutive_failures`` at or above the
  configured threshold AND the source is still enabled. Disabled
  sources are deliberately ignored — the operator turned them off,
  failures there aren't actionable.

The rule is exported as a factory rather than a module-level
``@register_rule`` so the supervisor's wiring stays explicit:
``app.py`` builds the rule with the live ``db_path`` and threshold,
then passes it to ``Supervisor(rules=[*BUILTIN_RULES, intel_rule])``.
That keeps Sentinel itself free of any hard dependency on the
intelligence subsystem — if the operator runs without intelligence
configured (no DB tables, etc.) the rule is just absent from the
supervisor's list.

What this rule does NOT do (deliberately, per Batch 3B scope):
  - Send Telegram alerts (notifier severity gating handles that)
  - Auto-disable sources
  - Create plans
  - Fetch anything from the network
  - Touch existing brain or memory state
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiosqlite

from core.db import connect
from core.logging import get_logger
from core.sentinel.contracts import Finding
from core.sentinel.rules import Rule, RuleContext

log = get_logger("pilkd.intelligence.sentinel_rules")

# Hard floor + ceiling on the threshold so a misconfigured env var
# can't either spam (threshold 0) or silently never fire (huge int).
_MIN_THRESHOLD = 1
_MAX_THRESHOLD = 1000

# Per-finding payload size cap. Sentinel's incident JSON serialises
# to SQLite, so keep details lean.
_MAX_DETAILS_FIELD_CHARS = 240


def make_intel_source_health_rule(
    *,
    db_path: Path,
    threshold: int = 5,
) -> Rule:
    """Build the source-health rule with a live ``db_path``.

    ``threshold`` is the consecutive-failures floor at which a
    Finding fires. Defaults to 5 so the rule is usable when the
    operator hasn't set ``PILK_INTELLIGENCE_BACKOFF_AFTER_FAILURES``.
    Bounded to [1, 1000].
    """
    bounded_threshold = max(_MIN_THRESHOLD, min(int(threshold), _MAX_THRESHOLD))

    async def intel_source_health(ctx: RuleContext) -> list[Finding]:
        # Defensive: if intel_sources doesn't exist (pre-v9 DB,
        # Sentinel constructed before migrations ran, test fixture,
        # whatever) the rule is a no-op. Never crash the whole rule
        # pass on a missing table.
        try:
            rows = await _fetch_failing_sources(
                db_path, bounded_threshold,
            )
        except _IntelTablesMissing:
            return []
        except (aiosqlite.Error, sqlite3.Error) as e:
            log.warning(
                "intel_source_health_query_failed",
                error=str(e),
            )
            return []

        if not rows:
            return []

        findings: list[Finding] = []
        for row in rows:
            slug = row["slug"]
            label = row["label"]
            kind = row["kind"]
            failures = row["consecutive_failures"]
            last_status = row["last_status"]
            last_checked = row["last_checked_at"]
            findings.append(
                Finding(
                    kind="intel_source_health",
                    # ``agent_name`` is the conventional Sentinel
                    # foreign key. Intelligence sources aren't agents
                    # but Sentinel UI groups by this field; use the
                    # source slug so the operator can see at a glance
                    # which source broke.
                    agent_name=f"intel:{slug}",
                    summary=(
                        f"Intelligence source '{label}' ({kind}) has "
                        f"{failures} consecutive failures "
                        f"(threshold {bounded_threshold})."
                    ),
                    details={
                        "source_id": row["id"],
                        "slug": slug,
                        "label": _truncate(label),
                        "kind": kind,
                        "consecutive_failures": failures,
                        "threshold": bounded_threshold,
                        "last_status": _truncate(last_status),
                        "last_checked_at": last_checked,
                        "url": _truncate(row["url"]),
                    },
                    # Stable per-source dedupe — Sentinel's window
                    # collapses repeats of the same key, so the same
                    # broken source doesn't produce a torrent of
                    # incidents while it's still broken. When the
                    # source recovers, ``consecutive_failures`` drops
                    # below threshold, the rule stops returning a
                    # Finding, and the existing incident naturally
                    # ages out without any explicit "resolved" call.
                    dedupe_key=f"intel_source_health:{row['id']}",
                )
            )
        return findings

    # The supervisor's status/log output uses ``rule.__name__`` so
    # surface a sensible identifier rather than the closure's auto-
    # generated name.
    intel_source_health.__name__ = "intel_source_health"  # type: ignore[attr-defined]
    return intel_source_health


# ── helpers ──────────────────────────────────────────────────────


class _IntelTablesMissing(Exception):
    """Raised when the intel_sources table isn't present yet."""


async def _fetch_failing_sources(
    db_path: Path, threshold: int,
) -> list[Any]:
    async with connect(db_path) as conn:
        try:
            cur = await conn.execute(
                """SELECT id, slug, label, kind, url,
                          consecutive_failures, last_status,
                          last_checked_at
                     FROM intel_sources
                    WHERE enabled = 1
                      AND consecutive_failures >= ?
                    ORDER BY consecutive_failures DESC, slug ASC""",
                (threshold,),
            )
        except (sqlite3.OperationalError, aiosqlite.OperationalError) as e:
            # SQLite raises OperationalError("no such table: ...")
            # when the migration hasn't run. Distinguish that from
            # other DB errors so the caller can return [] cleanly.
            if "no such table" in str(e).lower():
                raise _IntelTablesMissing() from e
            raise
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        return list(rows)


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_DETAILS_FIELD_CHARS:
        return value[:_MAX_DETAILS_FIELD_CHARS] + "…"
    return value


__all__ = ["make_intel_source_health_rule"]
