import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.orchestrator.orchestrator import OrchestratorBusyError
from core.registry.registry import AgentNotFoundError

router = APIRouter(prefix="/agents")


class RunBody(BaseModel):
    task: str


@router.get("")
async def list_agents(request: Request) -> dict:
    registry = request.app.state.agents
    if registry is None:
        return {"agents": []}
    return {"agents": await registry.list_rows()}


@router.post("/{name}/run")
async def run_agent(name: str, body: RunBody, request: Request) -> dict:
    orchestrator = request.app.state.orchestrator
    if orchestrator is None:
        raise HTTPException(
            status_code=503, detail="orchestrator offline (set ANTHROPIC_API_KEY)"
        )
    if orchestrator.running_plan_id is not None:
        raise HTTPException(status_code=409, detail="a plan is already running")
    task = body.task.strip()
    if not task:
        raise HTTPException(status_code=400, detail="task is empty")
    registry = request.app.state.agents
    if registry is None:
        raise HTTPException(status_code=503, detail="agent registry offline")
    try:
        registry.get(name)
    except AgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    tasks: set = request.app.state.orchestrator_tasks
    try:
        run = asyncio.create_task(orchestrator.agent_run(name, task))
    except OrchestratorBusyError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    tasks.add(run)
    run.add_done_callback(tasks.discard)
    return {"accepted": True, "agent": name, "task": task}
