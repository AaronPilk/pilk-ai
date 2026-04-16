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
    ) -> float:
        usd = price_usage(
            model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
        )
        meta = {
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
        }
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
