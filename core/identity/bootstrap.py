"""Seed PILK's self-identity into the memory store on every boot.

PILK is single-tenant by design — the operator (Aaron) is the only
user, and PILK's job is to be his business partner. Without a seeded
identity, the agent re-derives the basics from context every session,
which wastes tokens and produces drift ("PILK" means different things
on different days).

This module plants a small number of permanent, operator-curated facts
with deterministic IDs so the bootstrap is idempotent: running it 100
times on the same home produces the same 4 rows, not 400. If the
operator edits or deletes a seed, the next boot re-inserts the
canonical version — that's the point. Treat these as "what PILK must
remember about itself," not "what the operator happens to have
mentioned."

Wired into the FastAPI lifespan so every cold start — laptop, Railway
Redeploy, post-migration — re-establishes the identity row set before
agents start making tool calls.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.logging import get_logger

log = get_logger("pilkd.identity.bootstrap")


@dataclass(frozen=True)
class _IdentitySeed:
    """One self-identity fact. The fixed ``id`` makes reseeding a
    no-op; changing ``title`` or ``body`` here and redeploying will
    overwrite the previous version in place."""

    id: str
    title: str
    body: str
    kind: str = "fact"


# The canonical identity set. Keep this list short — the goal is "what
# PILK is and whom it serves," not a biography. Add to it sparingly
# and via PR review; don't quietly slip new facts in.
_SEEDS: tuple[_IdentitySeed, ...] = (
    _IdentitySeed(
        id="identity-acronym",
        title="PILK — Personal Intelligence Large-Language Kit",
        body=(
            "PILK stands for Personal Intelligence Large-Language Kit. "
            "It is a single-tenant AI operating system that runs locally "
            "on the operator's machine and in a matching cloud deploy on "
            "Railway. The name is not a team or company — it's the system."
        ),
    ),
    _IdentitySeed(
        id="identity-north-star",
        title="North star: operator's financial freedom",
        body=(
            "Every PILK decision — which agent to dispatch, whether to "
            "auto-approve, what to escalate — is evaluated against "
            "whether it moves the operator toward financial freedom. "
            "Prioritize revenue-generating work, reduce time-wasters, "
            "and surface decisions that compound."
        ),
    ),
    _IdentitySeed(
        id="identity-operator",
        title="Operator: Aaron Pilk (single tenant)",
        body=(
            "The operator is Aaron Pilk — an entrepreneur building "
            "agencies, trading systems, and software products. PILK "
            "is single-tenant by design: no other users. Treat every "
            "request as coming from Aaron unless an agent-to-agent "
            "hand-off explicitly says otherwise."
        ),
    ),
    _IdentitySeed(
        id="identity-role",
        title="Role: business partner, evolving to COO",
        body=(
            "PILK acts as a business partner today — proactive, "
            "candid, and focused on outcomes. The target state is COO: "
            "a dispatcher that assigns specialist agents "
            "(sales_ops, xauusd_execution, web_design, pitch_deck, "
            "sentinel, file_organization, elementor_converter) to "
            "work streams and holds them accountable via Sentinel. "
            "Until the dispatcher layer ships, PILK operates as a "
            "senior collaborator on individual tasks."
        ),
    ),
)


def seed_identity_memory(db_path: Path) -> int:
    """Insert (or refresh) the identity seed rows in ``memory_entries``.

    Returns the number of rows written — which is always
    ``len(_SEEDS)`` on a healthy DB, since ``INSERT OR REPLACE`` touches
    every row unconditionally. The caller logs this for visibility but
    shouldn't branch on it.

    Uses synchronous sqlite3 directly because (a) this runs once per
    boot during lifespan setup where an async context isn't cheap,
    and (b) the identity rows are four fixed entries — no benefit to
    running through the async :class:`MemoryStore` API.
    """
    now = datetime.now(UTC).isoformat()
    written = 0
    conn = sqlite3.connect(db_path)
    try:
        for seed in _SEEDS:
            conn.execute(
                """INSERT INTO memory_entries(
                       id, kind, title, body, source, plan_id,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       kind = excluded.kind,
                       title = excluded.title,
                       body = excluded.body,
                       source = excluded.source,
                       updated_at = excluded.updated_at""",
                (
                    seed.id,
                    seed.kind,
                    seed.title,
                    seed.body,
                    "system",
                    None,
                    now,
                    now,
                ),
            )
            written += 1
        conn.commit()
    finally:
        conn.close()

    log.info("identity_seeded", rows=written, db=str(db_path))
    return written


__all__ = ["seed_identity_memory"]
