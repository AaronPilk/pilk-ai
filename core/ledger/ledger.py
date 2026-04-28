"""Cost ledger: records every billable call and answers cost queries.

Every LLM call and every billable tool writes one `cost_entries` row with
(plan_id, step_id, agent_name, kind, model, tokens, usd). The Cost tab in
the dashboard queries `summary()` and `recent()` — no caching, the table
is small and these queries are cheap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.db import connect
from core.ledger.pricing import price_usage


@dataclass
class UsageSnapshot:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @classmethod
    def from_anthropic(cls, usage: Any) -> UsageSnapshot:
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(
                usage, "cache_creation_input_tokens", 0
            )
            or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )


class Ledger:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    async def record_llm(
        self,
        *,
        plan_id: str | None,
        step_id: str | None,
        agent_name: str | None,
        model: str,
        usage: UsageSnapshot,
        tier: str | None = None,
        tier_reason: str | None = None,
        tier_provider: str | None = None,
    ) -> float:
        """Persist one LLM call's cost row.

        ``tier`` / ``tier_reason`` / ``tier_provider`` are optional so
        legacy callers that haven't been updated still work. When set,
        they land in ``metadata_json`` alongside the cache-token stats
        so the Cost tab can pivot spend by tier + reason and the
        operator can see whether they're bleeding dollars because the
        classifier keeps picking PREMIUM.
        """
        usd = price_usage(
            model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
        )
        meta: dict[str, Any] = {
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
        }
        if tier is not None:
            meta["tier"] = tier
        if tier_reason is not None:
            meta["tier_reason"] = tier_reason
        if tier_provider is not None:
            meta["tier_provider"] = tier_provider
        async with connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO cost_entries
                  (plan_id, step_id, agent_name, kind, model,
                   input_tokens, output_tokens, usd, occurred_at, metadata_json)
                VALUES (?, ?, ?, 'llm', ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    step_id,
                    agent_name,
                    model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usd,
                    datetime.now(UTC).isoformat(),
                    json.dumps(meta),
                ),
            )
            if plan_id:
                await conn.execute(
                    "UPDATE plans SET actual_usd = COALESCE(actual_usd, 0) + ?, "
                    "updated_at = ? WHERE id = ?",
                    (usd, datetime.now(UTC).isoformat(), plan_id),
                )
            await conn.commit()
        return usd

    async def record_anthropic_response(
        self,
        *,
        model: str,
        response: Any,
        agent_name: str | None = None,
        tier_provider: str = "anthropic",
        tier: str | None = None,
        plan_id: str | None = None,
        step_id: str | None = None,
    ) -> float:
        """One-liner shortcut for ad-hoc Anthropic ``messages.create``
        calls outside the orchestrator's planner loop.

        Several tools call the Anthropic client directly (video
        analysis, best-of-N email drafting, memory consolidation,
        the self-capabilities refresher). Without this helper they
        bypass ``cost_entries`` entirely — the call hits Anthropic's
        billing but the dashboard sees nothing.

        Pulls the usage block off the response and routes through
        ``record_llm`` so cost rolls up the same way as planner
        spend. ``tier_provider`` defaults to ``anthropic`` because
        every direct ``messages.create`` against the Anthropic SDK
        is API-billed (subscription paths go through the Claude
        Code provider, not the SDK).
        """
        usage = UsageSnapshot.from_anthropic(
            getattr(response, "usage", None)
        )
        return await self.record_llm(
            plan_id=plan_id,
            step_id=step_id,
            agent_name=agent_name,
            model=model,
            usage=usage,
            tier=tier,
            tier_provider=tier_provider,
        )

    async def record_embedding(
        self,
        *,
        model: str,
        input_tokens: int,
        usd: float,
        agent_name: str | None = None,
        plan_id: str | None = None,
        step_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> float:
        """Persist one embedding-batch's cost row.

        Embeddings are conceptually similar to LLM calls (provider
        + model + tokens → USD) but they have no output tokens and
        no per-plan attribution by default. Kept as ``kind='embedding'``
        in ``cost_entries`` so the Cost tab can pivot on it without
        confusing it with chat LLM spend.
        """
        meta = dict(metadata or {})
        async with connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO cost_entries
                  (plan_id, step_id, agent_name, kind, model,
                   input_tokens, output_tokens, usd, occurred_at,
                   metadata_json)
                VALUES (?, ?, ?, 'embedding', ?, ?, 0, ?, ?, ?)
                """,
                (
                    plan_id,
                    step_id,
                    agent_name,
                    model,
                    int(input_tokens),
                    float(usd),
                    datetime.now(UTC).isoformat(),
                    json.dumps(meta),
                ),
            )
            await conn.commit()
        return float(usd)

    async def subscription_usage(
        self, *, window_hours: int = 5,
    ) -> dict[str, Any]:
        """Count turns billed against the Claude Max subscription within a
        rolling window.

        Anthropic doesn't expose the real subscription quota, so we
        estimate usage by counting every LLM entry with
        ``tier_provider='claude_code'`` that occurred in the last
        ``window_hours`` hours. Combined with a caller-tunable
        ``estimated_cap`` on the consumer side, that's close enough to
        drive a traffic-light UI indicator.

        Returns ``{count, window_hours, window_start, oldest_at}`` —
        the consumer supplies the cap and computes the percentage.
        """
        now = datetime.now(UTC)
        window_start = (now - timedelta(hours=window_hours)).isoformat()
        async with connect(self.db_path) as conn, conn.execute(
            """
            SELECT COUNT(*),
                   MIN(occurred_at)
            FROM cost_entries
            WHERE kind = 'llm'
              AND occurred_at >= ?
              AND json_extract(metadata_json, '$.tier_provider') = 'claude_code'
            """,
            (window_start,),
        ) as cur:
            row = await cur.fetchone()
        count = int(row[0]) if row and row[0] is not None else 0
        oldest = row[1] if row and row[1] else None
        return {
            "count": count,
            "window_hours": window_hours,
            "window_start": window_start,
            "oldest_at": oldest,
        }

    async def summary(self) -> dict[str, float]:
        now = datetime.now(UTC)
        day_start = (now - timedelta(days=1)).isoformat()
        week_start = (now - timedelta(days=7)).isoformat()
        month_start = (now - timedelta(days=30)).isoformat()
        async with connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT COALESCE(SUM(usd), 0) FROM cost_entries WHERE occurred_at >= ?",
                (day_start,),
            ) as cur:
                day = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT COALESCE(SUM(usd), 0) FROM cost_entries WHERE occurred_at >= ?",
                (week_start,),
            ) as cur:
                week = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT COALESCE(SUM(usd), 0) FROM cost_entries WHERE occurred_at >= ?",
                (month_start,),
            ) as cur:
                month = (await cur.fetchone())[0]
            async with conn.execute(
                "SELECT COALESCE(SUM(usd), 0) FROM cost_entries"
            ) as cur:
                total = (await cur.fetchone())[0]
        return {
            "day_usd": round(day, 6),
            "week_usd": round(week, 6),
            "month_usd": round(month, 6),
            "total_usd": round(total, 6),
        }

    async def agent_daily_usd(self, agent_name: str) -> float:
        """Return total $ spent by ``agent_name`` in the last rolling 24h.

        Used by the per-agent budget gate — an agent is refused a new
        run once its ``daily_usd`` cap has been spent in the window.
        Rolling 24h (not calendar day) so midnight doesn't reset a
        runaway agent.
        """
        if not agent_name:
            return 0.0
        day_start = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        async with connect(self.db_path) as conn, conn.execute(
            "SELECT COALESCE(SUM(usd), 0) FROM cost_entries "
            "WHERE agent_name = ? AND occurred_at >= ?",
            (agent_name, day_start),
        ) as cur:
            row = await cur.fetchone()
        return float(row[0] or 0.0)

    async def recent(self, limit: int = 50) -> list[dict]:
        async with connect(self.db_path) as conn, conn.execute(
            """
                SELECT id, plan_id, step_id, agent_name, kind, model,
                       input_tokens, output_tokens, usd, occurred_at
                FROM cost_entries
                ORDER BY id DESC LIMIT ?
                """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
