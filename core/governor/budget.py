"""Daily-spend budget tracker for the governor.

Reads rolled-up USD totals from the existing cost ledger — no new table.
The cap is a flat daily USD number from settings. We expose:

- today_usd() — sum of cost_entries for the local calendar day
- check()    — raises BudgetExceededError if today's spend >= cap
- summary()  — dict for the dashboard (spent, cap, warn_at)

An 80 % soft-warn threshold is returned so the UI can turn amber; the
hard stop is 100 %. We do not race-safely reserve future spend — each
LLM call runs, the cost records, and the NEXT call sees the new total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite


class BudgetExceededError(RuntimeError):
    def __init__(self, spent: float, cap: float):
        self.spent = spent
        self.cap = cap
        super().__init__(
            f"Daily budget reached: ${spent:.4f} spent of ${cap:.2f} cap"
        )


@dataclass
class BudgetSnapshot:
    spent_usd: float
    cap_usd: float
    warn_at_usd: float  # 80 % of cap
    is_over: bool
    is_warn: bool

    def to_public(self) -> dict:
        return {
            "spent_usd": round(self.spent_usd, 6),
            "cap_usd": round(self.cap_usd, 2),
            "warn_at_usd": round(self.warn_at_usd, 2),
            "is_over": self.is_over,
            "is_warn": self.is_warn,
        }


class DailyBudget:
    def __init__(self, db_path, cap_usd: float) -> None:
        self._db_path = str(db_path)
        self._cap_usd = float(cap_usd)

    @property
    def cap_usd(self) -> float:
        return self._cap_usd

    async def today_usd(self) -> float:
        """Sum of the cost ledger for the current UTC calendar day."""
        start = _start_of_utc_day()
        async with aiosqlite.connect(self._db_path) as db, db.execute(
            "SELECT COALESCE(SUM(usd), 0.0) FROM cost_entries WHERE occurred_at >= ?",
            (start,),
        ) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row else 0.0

    async def snapshot(self) -> BudgetSnapshot:
        spent = await self.today_usd()
        warn = self._cap_usd * 0.80
        return BudgetSnapshot(
            spent_usd=spent,
            cap_usd=self._cap_usd,
            warn_at_usd=warn,
            is_over=spent >= self._cap_usd,
            is_warn=spent >= warn,
        )

    async def check(self) -> None:
        if self._cap_usd <= 0:
            return  # unlimited
        spent = await self.today_usd()
        if spent >= self._cap_usd:
            raise BudgetExceededError(spent=spent, cap=self._cap_usd)


def _start_of_utc_day() -> str:
    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat()
