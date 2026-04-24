import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.orchestrator.orchestrator import OrchestratorBusyError
from core.policy import VALID_PROFILES
from core.registry.activation import evaluate as evaluate_activation
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
        # Activation status is derived from the same integrations list
        # above, but exposed as a single rolled-up flag the UI can
        # render as a chip — and the orchestrator uses for catalog
        # gating. Sentinel (supervisor) stays "active" regardless; it
        # doesn't participate in delegation either way.
        if manifest is None or row["name"] == "sentinel":
            row["activation"] = {
                "status": "active",
                "reason": "supervisor / no manifest",
                "missing": [],
            }
        else:
            report = evaluate_activation(
                manifest, secrets=secrets, accounts=accounts,
            )
            row["activation"] = {
                "status": report.status,
                "reason": report.reason,
                "missing": [
                    {
                        "kind": m.kind,
                        "name": m.name,
                        "label": m.label,
                        "role": m.role,
                    }
                    for m in report.missing
                ],
            }
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


@router.post("/{name}/test")
async def test_agent_integrations(name: str, request: Request) -> dict:
    """Run live probes against each of an agent's declared integrations.

    Verifies that stored credentials actually work — catches the
    "I pasted the API key but typo'd it" case that the plain
    ``configured=True`` check can't see. Returns one result per
    declared integration + a rolled-up pass/fail.

    Today's probe coverage: OAuth-based Google accounts
    (``gmail.users().getProfile()``). Other providers return
    ``status=\"no_probe\"`` until their probes are wired — this is a
    deliberate honesty: we say "key is set but not verified" rather
    than pretend everything works.
    """
    registry = request.app.state.agents
    if registry is None:
        raise HTTPException(status_code=503, detail="agent registry offline")
    try:
        manifest = registry.get(name)
    except AgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    secrets = getattr(request.app.state, "integration_secrets", None)
    accounts = getattr(request.app.state, "accounts", None)
    results: list[dict] = []
    overall = "active"
    for spec in manifest.integrations:
        entry: dict = {
            "name": spec.name,
            "kind": spec.kind,
            "label": spec.label,
            "role": spec.role,
        }
        if spec.kind == "api_key":
            val = secrets.get_value(spec.name) if secrets is not None else None
            if val is None:
                entry.update(status="missing", detail="API key not set.")
                overall = "needs_setup"
            else:
                entry.update(
                    status="no_probe",
                    detail=(
                        "Key is set but no live probe wired for this "
                        "provider yet — accepting as configured."
                    ),
                )
        elif spec.kind == "oauth":
            role = spec.role or "user"
            account = (
                accounts.default(spec.name, role) if accounts is not None
                else None
            )
            if account is None:
                entry.update(status="missing", detail="OAuth account not linked.")
                overall = "needs_setup"
            elif spec.name == "google":
                probe = await _probe_google(accounts, role)
                entry.update(probe)
                if probe["status"] != "ok":
                    overall = "probe_failing" if overall != "needs_setup" else overall
            else:
                entry.update(
                    status="no_probe",
                    detail=(
                        "OAuth account linked but no live probe wired "
                        "for this provider yet — accepting as configured."
                    ),
                )
        else:
            entry.update(status="unknown_kind", detail=f"unsupported kind: {spec.kind}")
        results.append(entry)
    return {
        "agent": name,
        "overall": overall,
        "checked_at": __import__("datetime").datetime.now(
            __import__("datetime").UTC
        ).isoformat(),
        "results": results,
    }


async def _probe_google(accounts, role: str) -> dict:
    """Cheap authenticated Google probe via Gmail ``users().getProfile``.

    Covers Gmail scopes the most common agents need. If the link
    exists but the token was revoked, this surfaces a concrete error
    the operator can act on ("reconnect in Settings") rather than
    silently waiting for an agent run to fail.
    """
    try:
        from core.integrations.google.oauth import credentials_from_blob
    except Exception as e:  # pragma: no cover — defensive import
        return {
            "status": "probe_error",
            "detail": f"could not import google oauth helper: {e}",
        }
    account = accounts.default("google", role)
    if account is None:
        return {"status": "missing", "detail": "account not linked"}
    tokens = accounts.load_tokens(account.account_id)
    if tokens is None:
        return {"status": "missing", "detail": "tokens unreadable"}
    blob = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "client_id": tokens.client_id,
        "client_secret": tokens.client_secret,
        "scopes": tokens.scopes,
        "token_uri": tokens.token_uri,
        "email": account.email,
    }
    try:
        creds = credentials_from_blob(blob)
        service = await asyncio.to_thread(creds.build, "gmail", "v1")
        profile = await asyncio.to_thread(
            lambda: service.users().getProfile(userId="me").execute()
        )
    except Exception as e:
        return {
            "status": "probe_failing",
            "detail": f"live call failed: {type(e).__name__}: {e}",
        }
    return {
        "status": "ok",
        "detail": f"authenticated as {profile.get('emailAddress', '?')}",
    }
