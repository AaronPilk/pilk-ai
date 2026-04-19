import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.orchestrator.orchestrator import OrchestratorBusyError
from core.policy import VALID_PROFILES
from core.registry.registry import AgentNotFoundError

router = APIRouter(prefix="/agents")


class RunBody(BaseModel):
    task: str


class PolicyBody(BaseModel):
    profile: str


@router.get("")
async def list_agents(request: Request) -> dict:
    registry = request.app.state.agents
    store = getattr(request.app.state, "agent_policies", None)
    policies = store.all() if store is not None else {}
    if registry is None:
        return {"agents": [], "profiles": sorted(VALID_PROFILES)}
    rows = await registry.list_rows()

    # Stamp each row with per-agent integration requirements + whether
    # they're configured. The UI renders these inline so the operator
    # can paste keys / Connect OAuth without bouncing to Settings.
    secrets = getattr(request.app.state, "integration_secrets", None)
    accounts = getattr(request.app.state, "accounts", None)
    manifests = registry.manifests()
    for row in rows:
        manifest = manifests.get(row["name"])
        row["autonomy_profile"] = policies.get(row["name"], "assistant")
        row["integrations"] = _integrations_for(manifest, secrets, accounts)
    return {"agents": rows, "profiles": sorted(VALID_PROFILES)}


def _integrations_for(manifest, secrets, accounts) -> list[dict]:
    """Map a manifest's declared integrations → UI-ready dicts.

    Missing stores (local dev without Settings seeded) fall back to
    ``configured=False`` rather than raising — the UI still renders the
    row, just with the "Needs setup" affordance.
    """
    if manifest is None or not manifest.integrations:
        return []
    out: list[dict] = []
    for spec in manifest.integrations:
        configured = False
        if spec.kind == "api_key" and secrets is not None:
            configured = secrets.get_value(spec.name) is not None
        elif spec.kind == "oauth" and accounts is not None:
            role = spec.role or "user"
            configured = accounts.default(spec.name, role) is not None
        out.append(
            {
                "name": spec.name,
                "kind": spec.kind,
                "label": spec.label,
                "role": spec.role,
                "scopes": list(spec.scopes),
                "docs_url": spec.docs_url,
                "configured": configured,
            }
        )
    return out


@router.post("/{name}/policy")
async def set_agent_policy(
    name: str, body: PolicyBody, request: Request
) -> dict:
    registry = request.app.state.agents
    store = getattr(request.app.state, "agent_policies", None)
    if store is None:
        raise HTTPException(status_code=503, detail="policy store offline")
    if registry is not None:
        try:
            registry.get(name)
        except AgentNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    try:
        profile = await store.set(name, body.profile.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"agent": name, "profile": profile}


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
