"""REST surface for the approval + trust layer.

  GET  /approvals                list pending + recent items
  POST /approvals/{id}/approve   body: {reason?, trust?: {scope, ttl_seconds}}
  POST /approvals/{id}/reject    body: {reason?}
  POST /approvals/batch/approve  body: {reason?} → approves every
                                  non-financial pending item in one shot
  GET  /trust                    list live trust rules
  DELETE /trust/{id}             revoke a trust rule

The dashboard prefers WS events for updates; these endpoints exist so
actions are idempotent REST calls that don't depend on a WS round-trip.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()


class TrustScope(BaseModel):
    scope: Literal["none", "agent", "agent+args"] = "agent+args"
    ttl_seconds: int = Field(gt=0, le=24 * 60 * 60)


class ApproveBody(BaseModel):
    reason: str | None = None
    trust: TrustScope | None = None


class RejectBody(BaseModel):
    reason: str | None = None


class BatchApproveBody(BaseModel):
    reason: str | None = None


@router.get("/approvals")
async def list_approvals(request: Request) -> dict[str, Any]:
    mgr = request.app.state.approvals
    if mgr is None:
        return {"pending": [], "recent": []}
    pending = await mgr.pending_list()
    recent = await mgr.recent(limit=50)
    return {"pending": pending, "recent": recent}


@router.post("/approvals/{approval_id}/approve")
async def approve_one(
    approval_id: str, body: ApproveBody, request: Request
) -> dict[str, Any]:
    mgr = request.app.state.approvals
    if mgr is None:
        raise HTTPException(status_code=503, detail="approval manager offline")
    try:
        decision = await mgr.approve(
            approval_id,
            reason=body.reason or "",
            trust=body.trust.model_dump() if body.trust else None,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {
        "decision": decision.decision,
        "reason": decision.reason,
        "trust_rule": decision.trust_rule.public_dict()
        if decision.trust_rule
        else None,
    }


@router.post("/approvals/{approval_id}/reject")
async def reject_one(
    approval_id: str, body: RejectBody, request: Request
) -> dict[str, Any]:
    mgr = request.app.state.approvals
    if mgr is None:
        raise HTTPException(status_code=503, detail="approval manager offline")
    try:
        decision = await mgr.reject(approval_id, reason=body.reason or "")
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"decision": decision.decision, "reason": decision.reason}


@router.post("/approvals/batch/approve")
async def approve_all(
    body: BatchApproveBody, request: Request
) -> dict[str, Any]:
    mgr = request.app.state.approvals
    if mgr is None:
        raise HTTPException(status_code=503, detail="approval manager offline")
    ids = await mgr.approve_batch(reason=body.reason or "")
    return {"approved": ids, "count": len(ids)}


@router.get("/trust")
async def list_trust(request: Request) -> dict[str, Any]:
    store = request.app.state.trust
    if store is None:
        return {"rules": []}
    return {"rules": [r.public_dict() for r in store.list()]}


@router.delete("/trust/{rule_id}")
async def revoke_trust(rule_id: str, request: Request) -> dict[str, Any]:
    store = request.app.state.trust
    hub = request.app.state.hub
    if store is None:
        raise HTTPException(status_code=503, detail="trust store offline")
    removed = store.revoke(rule_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"no such rule: {rule_id}")
    if hub is not None:
        await hub.broadcast("trust.revoked", {"id": rule_id})
    return {"revoked": rule_id}
