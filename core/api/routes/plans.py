from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/plans")


class CancelBody(BaseModel):
    reason: str | None = None


@router.get("")
async def list_plans(request: Request) -> dict:
    plans = await request.app.state.plans.list_plans()
    return {
        "plans": plans,
        "running_plan_id": request.app.state.orchestrator.running_plan_id
        if request.app.state.orchestrator
        else None,
    }


@router.get("/{plan_id}")
async def get_plan(plan_id: str, request: Request) -> dict:
    try:
        return await request.app.state.plans.get_plan(plan_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{plan_id}/cancel")
async def cancel_plan(plan_id: str, body: CancelBody, request: Request) -> dict:
    """Stop a running plan.

    Arms orchestrator cancellation, force-resolves any approval the
    plan is waiting on, and closes any browser sessions the plan owns.
    Returns 409 if the plan isn't the currently running one.
    """
    orchestrator = request.app.state.orchestrator
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="orchestrator offline")
    reason = (body.reason or "cancelled by user").strip() or "cancelled by user"
    armed = await orchestrator.cancel_plan(plan_id, reason=reason)
    if not armed:
        raise HTTPException(
            status_code=409,
            detail=f"plan {plan_id} is not the currently running plan",
        )
    closed: list[str] = []
    browser_sessions = getattr(request.app.state, "browser_sessions", None)
    if browser_sessions is not None:
        try:
            closed = await browser_sessions.close_for_plan(plan_id)
        except Exception:  # pragma: no cover — best-effort cleanup
            closed = []
    return {
        "cancelled": True,
        "plan_id": plan_id,
        "reason": reason,
        "closed_browser_sessions": closed,
    }


@router.post("/cancel-all")
async def cancel_all(request: Request) -> dict:
    """Emergency stop — cancel the running plan and close every browser.

    Plans run one at a time, so this really cancels at most one plan;
    the intent is a single red-button surface in the UI.
    """
    orchestrator = request.app.state.orchestrator
    plan_id = orchestrator.running_plan_id if orchestrator is not None else None
    cancelled_plan = None
    if plan_id is not None:
        await orchestrator.cancel_plan(plan_id, reason="emergency stop")
        cancelled_plan = plan_id
    browser_sessions = getattr(request.app.state, "browser_sessions", None)
    closed: list[str] = []
    if browser_sessions is not None:
        try:
            for sess in list(browser_sessions.active()):
                await browser_sessions.close(sess.id)
                closed.append(sess.id)
        except Exception:  # pragma: no cover — best-effort
            pass
    return {"cancelled_plan_id": cancelled_plan, "closed_browser_sessions": closed}
