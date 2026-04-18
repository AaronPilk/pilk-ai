"""Approval queue.

When the policy gate returns `APPROVE`, the gateway opens a request here
and awaits a resolution. The request:

  * persists to the `approvals` table so a dashboard reload can still see
    pending items,
  * broadcasts `approval.created` so every connected dashboard shows it
    in real time,
  * blocks on an asyncio.Future until the user resolves it via the REST
    endpoint (or a batch approval, or a trust rule installed while it
    was pending).

On resolution the row is updated with decided_at + reason and
`approval.resolved` is emitted.

Design notes:

- Pending requests are keyed by id in memory; the DB is a durable mirror.
- An approved request may install a TrustRule if the user chose a
  "remember this for N minutes" scope. The financial sub-policy overrides
  that — deposit/withdraw/transfer can never install a trust rule.
- `approve_batch` lets the user green-light every currently pending
  request in one click (with the same caveat — financial items stay).
- Nothing here knows about the gateway or orchestrator; they see a
  simple Future.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.db import connect
from core.logging import get_logger
from core.policy import financial
from core.policy.risk import RiskClass
from core.policy.trust import TrustRule, TrustStore

log = get_logger("pilkd.approvals")

Broadcaster = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class ApprovalDecision:
    decision: str                         # "approved" | "rejected" | "expired"
    reason: str = ""
    trust_rule: TrustRule | None = None


@dataclass
class ApprovalRequest:
    id: str
    plan_id: str | None
    step_id: str | None
    agent_name: str | None
    tool_name: str
    args: dict[str, Any]
    risk_class: RiskClass
    reason: str
    created_at: str
    bypass_trust: bool = False
    future: asyncio.Future[ApprovalDecision] = field(
        default_factory=asyncio.Future, repr=False
    )

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "agent_name": self.agent_name,
            "tool_name": self.tool_name,
            "args": self.args,
            "risk_class": self.risk_class.value,
            "reason": self.reason,
            "created_at": self.created_at,
            "bypass_trust": self.bypass_trust,
        }


class ApprovalManager:
    def __init__(
        self,
        *,
        db_path: Path,
        trust_store: TrustStore,
        broadcast: Broadcaster | None = None,
    ) -> None:
        self.db_path = db_path
        self.trust = trust_store
        self.broadcast = broadcast or (lambda _t, _p: _noop())
        self._pending: dict[str, ApprovalRequest] = {}
        self._lock = asyncio.Lock()

    # ── Requests ─────────────────────────────────────────────────────

    async def request(
        self,
        *,
        plan_id: str | None,
        step_id: str | None,
        agent_name: str | None,
        tool_name: str,
        args: dict[str, Any],
        risk_class: RiskClass,
        reason: str,
        bypass_trust: bool = False,
    ) -> ApprovalRequest:
        loop = asyncio.get_running_loop()
        req = ApprovalRequest(
            id=f"appr_{uuid.uuid4().hex[:12]}",
            plan_id=plan_id,
            step_id=step_id,
            agent_name=agent_name,
            tool_name=tool_name,
            args=dict(args),
            risk_class=risk_class,
            reason=reason,
            created_at=datetime.now(UTC).isoformat(),
            bypass_trust=bypass_trust,
            future=loop.create_future(),
        )
        async with self._lock:
            self._pending[req.id] = req
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO approvals(id, plan_id, step_id, agent_name, "
                "risk_class, tool, args_json, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    req.id,
                    req.plan_id,
                    req.step_id,
                    req.agent_name,
                    req.risk_class.value,
                    req.tool_name,
                    json.dumps(req.args),
                    req.created_at,
                ),
            )
            await conn.commit()
        log.info(
            "approval_requested",
            id=req.id,
            tool=tool_name,
            risk=risk_class.value,
            agent=agent_name,
        )
        await self.broadcast("approval.created", req.public_dict())
        return req

    # ── Resolutions ─────────────────────────────────────────────────

    async def approve(
        self,
        approval_id: str,
        *,
        reason: str = "",
        trust: dict[str, Any] | None = None,
    ) -> ApprovalDecision:
        async with self._lock:
            req = self._pending.pop(approval_id, None)
        if req is None:
            raise LookupError(f"approval not pending: {approval_id}")

        rule: TrustRule | None = None
        if trust and not req.bypass_trust:
            ttl = int(trust.get("ttl_seconds") or 0)
            if ttl > 0:
                scope = str(trust.get("scope") or "agent+args")
                args_matcher: dict[str, Any] = {}
                if scope == "agent+args":
                    args_matcher = dict(req.args)
                elif scope == "agent":
                    args_matcher = {}
                elif scope == "none":
                    args_matcher = {}
                    ttl = 0  # no rule
                if ttl > 0:
                    rule = self.trust.add(
                        agent_name=req.agent_name,
                        tool_name=req.tool_name,
                        args_matcher=args_matcher,
                        ttl_seconds=ttl,
                        reason=reason or None,
                    )
                    await self._audit_trust(
                        rule, approval_id=approval_id, ttl_seconds=ttl
                    )
        elif trust and req.bypass_trust:
            log.info(
                "approval_trust_denied",
                id=approval_id,
                tool=req.tool_name,
                detail="financial sub-policy disallows trust rules for this tool",
            )

        await self._mark_resolved(approval_id, "approved", reason)
        decision = ApprovalDecision(decision="approved", reason=reason, trust_rule=rule)
        if not req.future.done():
            req.future.set_result(decision)
        await self.broadcast(
            "approval.resolved",
            {
                "id": approval_id,
                "decision": "approved",
                "reason": reason,
                "trust_rule": rule.public_dict() if rule else None,
            },
        )
        if rule is not None:
            await self.broadcast("trust.updated", {"rule": rule.public_dict()})
        log.info(
            "approval_granted",
            id=approval_id,
            tool=req.tool_name,
            trust=bool(rule),
        )
        return decision

    async def reject(self, approval_id: str, *, reason: str = "") -> ApprovalDecision:
        async with self._lock:
            req = self._pending.pop(approval_id, None)
        if req is None:
            raise LookupError(f"approval not pending: {approval_id}")
        await self._mark_resolved(approval_id, "rejected", reason)
        decision = ApprovalDecision(decision="rejected", reason=reason)
        if not req.future.done():
            req.future.set_result(decision)
        await self.broadcast(
            "approval.resolved",
            {"id": approval_id, "decision": "rejected", "reason": reason},
        )
        log.info("approval_rejected", id=approval_id, tool=req.tool_name)
        return decision

    async def cancel_plan(self, plan_id: str, *, reason: str = "") -> list[str]:
        """Force-resolve every pending approval tied to `plan_id`.

        Used when the user cancels a running plan; the orchestrator is
        blocked on these futures and won't make progress otherwise. Each
        pending request is resolved with decision='cancelled' so the
        gateway treats it as a refusal and the plan unwinds.
        """
        async with self._lock:
            ids = [
                rid for rid, r in self._pending.items() if r.plan_id == plan_id
            ]
            reqs = [self._pending.pop(rid) for rid in ids]
        cancelled: list[str] = []
        for req in reqs:
            await self._mark_resolved(req.id, "cancelled", reason)
            decision = ApprovalDecision(decision="cancelled", reason=reason)
            if not req.future.done():
                req.future.set_result(decision)
            await self.broadcast(
                "approval.resolved",
                {"id": req.id, "decision": "cancelled", "reason": reason},
            )
            log.info("approval_cancelled", id=req.id, tool=req.tool_name)
            cancelled.append(req.id)
        return cancelled

    async def approve_batch(self, *, reason: str = "") -> list[str]:
        """Approve every currently pending request whose tool is whitelistable.

        Financial items that demand a fresh per-call decision are left
        pending — bulk-approving them would defeat the sub-policy's whole
        point.
        """
        async with self._lock:
            ids = [rid for rid, r in self._pending.items() if not r.bypass_trust]
        approved: list[str] = []
        for rid in ids:
            try:
                await self.approve(rid, reason=reason or "batch approved")
                approved.append(rid)
            except LookupError:
                continue
        return approved

    # ── Views ───────────────────────────────────────────────────────

    async def pending_list(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [r.public_dict() for r in self._pending.values()]

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        async with connect(self.db_path) as conn, conn.execute(
            "SELECT id, plan_id, step_id, agent_name, risk_class, tool, "
            "args_json, status, created_at, decided_at, decision_reason "
            "FROM approvals ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            raw = d.pop("args_json", None)
            try:
                d["args"] = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                d["args"] = {}
            out.append(d)
        return out

    # ── Internals ───────────────────────────────────────────────────

    async def _mark_resolved(
        self, approval_id: str, status: str, reason: str
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, "
                "decision_reason = ? WHERE id = ?",
                (status, now, reason or None, approval_id),
            )
            await conn.commit()

    async def _audit_trust(
        self, rule: TrustRule, *, approval_id: str, ttl_seconds: int
    ) -> None:
        expires_iso = datetime.fromtimestamp(rule.expires_at, UTC).isoformat()
        created_iso = datetime.fromtimestamp(rule.created_at, UTC).isoformat()
        async with connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO trust_audit(id, agent_name, tool_name, args_json, "
                "ttl_seconds, expires_at, created_at, created_by, reason, "
                "approval_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule.id,
                    rule.agent_name,
                    rule.tool_name,
                    json.dumps(rule.args_matcher),
                    ttl_seconds,
                    expires_iso,
                    created_iso,
                    rule.created_by,
                    rule.reason,
                    approval_id,
                ),
            )
            await conn.commit()


async def _noop() -> None:
    return None


__all__ = [
    "ApprovalDecision",
    "ApprovalManager",
    "ApprovalRequest",
    "financial",
]
