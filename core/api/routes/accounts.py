"""Connected accounts REST surface.

  GET    /integrations/providers                         provider catalog
  GET    /integrations/accounts                          list connected accounts
  GET    /integrations/accounts/{id}                     one account
  DELETE /integrations/accounts/{id}                     remove
  POST   /integrations/accounts/{id}/default             set as default for its role
  POST   /integrations/accounts/oauth/start              body: {provider, role, make_default?}
                                                          → {auth_url, state}
  GET    /integrations/accounts/oauth/callback           provider redirects here;
                                                          returns an HTML "close this tab" page

All provider-specific behavior (Google, Slack, …) is declarative — the
generic flow in `core.integrations.oauth_flow` consumes `OAuthProvider`
metadata. Adding a new provider does not touch this file.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from core.identity import AccountsStore, GrantsStore
from core.integrations.oauth_flow import OAuthFlowManager
from core.integrations.provider import ProviderRegistry
from core.logging import get_logger

log = get_logger("pilkd.routes.accounts")

router = APIRouter(prefix="/integrations")


class OAuthStartBody(BaseModel):
    provider: str = Field(..., description="Provider name, e.g. 'google'.")
    role: str = Field(..., description="'system' (PILK acts as itself) or 'user'.")
    make_default: bool = Field(
        default=False,
        description="If true, set this account as the default for (provider, role) on success.",
    )
    scope_groups: list[str] | None = Field(
        default=None,
        description=(
            "Which named scope groups to request (e.g. ['mail', 'drive']). "
            "If omitted, the provider's default group set is used."
        ),
    )


def _store(request: Request) -> AccountsStore:
    store = getattr(request.app.state, "accounts", None)
    if store is None:
        raise HTTPException(status_code=503, detail="accounts store offline")
    return store


def _providers(request: Request) -> ProviderRegistry:
    reg = getattr(request.app.state, "oauth_providers", None)
    if reg is None:
        raise HTTPException(status_code=503, detail="provider registry offline")
    return reg


def _flow(request: Request) -> OAuthFlowManager:
    flow = getattr(request.app.state, "oauth_flow", None)
    if flow is None:
        raise HTTPException(status_code=503, detail="oauth flow offline")
    return flow


def _grants(request: Request) -> GrantsStore:
    grants = getattr(request.app.state, "grants", None)
    if grants is None:
        raise HTTPException(status_code=503, detail="grants store offline")
    return grants


async def _broadcast(request: Request, event_type: str, payload: dict) -> None:
    hub = getattr(request.app.state, "hub", None)
    if hub is not None:
        await hub.broadcast(event_type, payload)


# ── provider catalog ──────────────────────────────────────────────────


@router.get("/providers")
async def list_providers(request: Request) -> dict:
    registry = _providers(request)
    out: list[dict] = []
    for p in registry.all():
        out.append(
            {
                "name": p.name,
                "label": p.label,
                "supports_roles": list(p.supports_roles),
                "scopes": [
                    {
                        "name": s.name,
                        "label": s.label,
                        "risk": s.risk_hint.value,
                    }
                    for s in p.scope_catalog.values()
                ],
                "scope_groups": [
                    {"name": name, "label": label}
                    for name, label in p.scope_groups.items()
                ],
                "default_scope_groups": list(p.default_scope_groups),
            }
        )
    return {"providers": out}


# ── accounts ──────────────────────────────────────────────────────────


@router.get("/accounts")
async def list_accounts(
    request: Request,
    provider: str | None = None,
    role: str | None = None,
) -> dict:
    store = _store(request)
    accounts = store.list(
        provider=provider,
        role=role if role in ("system", "user") else None,  # type: ignore[arg-type]
    )
    defaults: dict[str, str] = {}
    for account in accounts:
        key = f"{account.provider}:{account.role}"
        if key not in defaults:
            aid = store.default_id(account.provider, account.role)  # type: ignore[arg-type]
            if aid is not None:
                defaults[key] = aid
    return {
        "accounts": [a.public_dict() for a in accounts],
        "defaults": defaults,
    }


@router.get("/accounts/{account_id}")
async def get_account(account_id: str, request: Request) -> dict:
    account = _store(request).get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"no such account: {account_id}")
    return account.public_dict()


@router.delete("/accounts/{account_id}")
async def remove_account(account_id: str, request: Request) -> dict:
    store = _store(request)
    if not store.remove(account_id):
        raise HTTPException(status_code=404, detail=f"no such account: {account_id}")
    # Drop the removed account from every grant list; agents silently lose
    # access rather than erroring on the next call.
    grants = getattr(request.app.state, "grants", None)
    if grants is not None:
        grants.remove_account_everywhere(account_id)
    await _broadcast(request, "account.removed", {"account_id": account_id})
    return {"removed": account_id}


@router.post("/accounts/{account_id}/default")
async def set_default(account_id: str, request: Request) -> dict:
    store = _store(request)
    if not store.set_default(account_id):
        raise HTTPException(status_code=404, detail=f"no such account: {account_id}")
    await _broadcast(request, "account.default_changed", {"account_id": account_id})
    return {"default": account_id}


# ── Agent access grants ───────────────────────────────────────────────


@router.get("/grants")
async def list_grants(request: Request) -> dict:
    grants = _grants(request)
    return {
        "grants": {
            name: {
                "agent_name": g.agent_name,
                "accounts": g.accounts,
                "granted_at": g.granted_at,
                "granted_by": g.granted_by,
            }
            for name, g in grants.all().items()
        },
    }


@router.get("/accounts/{account_id}/agents")
async def agents_for_account(account_id: str, request: Request) -> dict:
    _ = _store(request).get(account_id)
    grants = _grants(request)
    return {"account_id": account_id, "agents": grants.agents_for(account_id)}


@router.post("/accounts/{account_id}/agents/{agent_name}")
async def grant_access(
    account_id: str, agent_name: str, request: Request
) -> dict:
    store = _store(request)
    if store.get(account_id) is None:
        raise HTTPException(status_code=404, detail=f"no such account: {account_id}")
    grants = _grants(request)
    added = grants.grant(agent_name, account_id)
    await _broadcast(
        request,
        "agent.grant_added",
        {"agent": agent_name, "account_id": account_id},
    )
    return {"granted": added, "agent": agent_name, "account_id": account_id}


@router.delete("/accounts/{account_id}/agents/{agent_name}")
async def revoke_access(
    account_id: str, agent_name: str, request: Request
) -> dict:
    grants = _grants(request)
    removed = grants.revoke(agent_name, account_id)
    if removed:
        await _broadcast(
            request,
            "agent.grant_removed",
            {"agent": agent_name, "account_id": account_id},
        )
    return {"revoked": removed, "agent": agent_name, "account_id": account_id}


# ── OAuth flow ────────────────────────────────────────────────────────


@router.post("/accounts/oauth/start")
async def oauth_start(body: OAuthStartBody, request: Request) -> dict:
    flow = _flow(request)
    if body.role not in ("system", "user"):
        raise HTTPException(status_code=400, detail=f"unknown role: {body.role}")
    try:
        return flow.start(
            provider_name=body.provider,
            role=body.role,  # type: ignore[arg-type]
            make_default=body.make_default,
            scope_groups=body.scope_groups,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/accounts/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    if error:
        return HTMLResponse(_error_page(error), status_code=400)
    if not code or not state:
        return HTMLResponse(_error_page("missing code or state"), status_code=400)
    flow = _flow(request)
    try:
        account = await flow.complete(code=code, state=state)
    except ValueError as e:
        return HTMLResponse(_error_page(str(e)), status_code=400)
    except RuntimeError as e:
        log.exception("oauth_callback_failed")
        return HTMLResponse(_error_page(str(e)), status_code=500)
    await _broadcast(
        request,
        "account.linked",
        {
            "account_id": account.account_id,
            "provider": account.provider,
            "role": account.role,
            "email": account.email,
        },
    )
    return HTMLResponse(_success_page(account.email or account.account_id))


# ── tiny HTML pages (no templating, no deps) ──────────────────────────


_BASE = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>PILK · connected account</title>
    <style>
      html, body { margin: 0; height: 100%%; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
      body { display: flex; align-items: center; justify-content: center; background: #07080b; color: #eef0f5; }
      .card { padding: 32px 40px; border-radius: 20px; background: rgba(22,24,34,.62); border: 1px solid rgba(255,255,255,.08); max-width: 420px; text-align: center; }
      h1 { font-size: 18px; margin: 0 0 10px; font-weight: 600; }
      p { font-size: 13px; color: #a2a8b8; line-height: 1.5; margin: 0; }
      .ok { color: #65d19b; }
      .err { color: #ff6b6b; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1 class="%(tone)s">%(title)s</h1>
      <p>%(body)s</p>
    </div>
    <script>
      // If this tab was opened from the PILK dashboard, close automatically.
      setTimeout(function () { try { window.close(); } catch (e) {} }, 1500);
    </script>
  </body>
</html>
"""


def _success_page(who: str) -> str:
    return _BASE % {
        "tone": "ok",
        "title": "Connected",
        "body": (
            f"Linked <strong>{_escape(who)}</strong>. You can close this tab; the "
            "PILK dashboard will update automatically."
        ),
    }


def _error_page(msg: str) -> str:
    return _BASE % {
        "tone": "err",
        "title": "Couldn't connect",
        "body": _escape(msg),
    }


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
