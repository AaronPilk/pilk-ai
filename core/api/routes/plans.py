from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/plans")


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
