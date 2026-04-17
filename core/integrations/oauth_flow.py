"""Generic OAuth 2.0 authorization-code flow.

One implementation, driven entirely by `OAuthProvider` metadata. Each
provider file declares its auth/token URLs, scope catalog, and profile
fetcher; this module does the actual state management and token
exchange.

The `client_loader` callback resolves provider name → (client_id,
client_secret). `app.py` wires this at boot — for Google, it reads
`pilk-google-client.json`; a future Slack provider reads env vars.

Token exchange uses stdlib `urllib.request` so we don't add a new
HTTP dependency just for OAuth. It's one POST; the heavy lifting
afterwards (actual API calls) still runs through provider-specific
clients (googleapiclient, slack_sdk, etc.).
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Literal

from core.identity import AccountsStore, ConnectedAccount
from core.identity.accounts import OAuthTokens
from core.integrations.provider import OAuthProvider, ProviderRegistry
from core.logging import get_logger

log = get_logger("pilkd.oauth")

Role = Literal["system", "user"]
STATE_TTL_SECONDS = 15 * 60


@dataclass
class _PendingState:
    state: str
    provider: str
    role: Role
    redirect_uri: str
    created_at: float
    make_default: bool


class OAuthFlowManager:
    def __init__(
        self,
        *,
        providers: ProviderRegistry,
        accounts: AccountsStore,
        client_loader: Callable[[str], tuple[str, str] | None],
        public_base_url: str,
        callback_path: str = "/integrations/accounts/oauth/callback",
    ) -> None:
        self._providers = providers
        self._accounts = accounts
        self._client_loader = client_loader
        self._callback_url = public_base_url.rstrip("/") + callback_path
        self._pending: dict[str, _PendingState] = {}
        self._lock = Lock()

    # ── begin ─────────────────────────────────────────────────────

    def start(
        self,
        *,
        provider_name: str,
        role: Role,
        make_default: bool = False,
    ) -> dict:
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ValueError(f"unknown provider: {provider_name}")
        if role not in provider.supports_roles:
            raise ValueError(
                f"provider {provider_name} does not support role {role}"
            )
        client = self._client_loader(provider_name)
        if client is None:
            raise RuntimeError(
                f"no OAuth client configured for provider {provider_name}"
            )
        client_id, _client_secret = client
        state = secrets.token_urlsafe(24)
        pending = _PendingState(
            state=state,
            provider=provider_name,
            role=role,
            redirect_uri=self._callback_url,
            created_at=time.time(),
            make_default=make_default,
        )
        with self._lock:
            self._purge_expired_locked()
            self._pending[state] = pending

        params = {
            "client_id": client_id,
            "redirect_uri": self._callback_url,
            "response_type": "code",
            "scope": " ".join(provider.scopes_for_role(role)),
            "state": state,
            **provider.extra_auth_params,
        }
        auth_url = f"{provider.auth_url}?{urllib.parse.urlencode(params)}"
        log.info(
            "oauth_begin",
            provider=provider_name,
            role=role,
            state=state[:8] + "…",
        )
        return {"auth_url": auth_url, "state": state, "redirect_uri": self._callback_url}

    # ── complete ──────────────────────────────────────────────────

    async def complete(self, *, code: str, state: str) -> ConnectedAccount:
        with self._lock:
            self._purge_expired_locked()
            pending = self._pending.pop(state, None)
        if pending is None:
            raise ValueError("invalid or expired state token")
        provider = self._providers.get(pending.provider)
        if provider is None:
            raise RuntimeError(f"provider vanished: {pending.provider}")
        client = self._client_loader(pending.provider)
        if client is None:
            raise RuntimeError(f"no OAuth client for {pending.provider}")
        client_id, client_secret = client

        token_response = _exchange_code(
            token_url=provider.token_url,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=pending.redirect_uri,
        )
        refresh_token = token_response.get("refresh_token")
        if not refresh_token:
            raise RuntimeError(
                "provider did not return a refresh_token — remove access at "
                "the provider's permission settings and re-link so consent "
                "is prompted again"
            )
        granted_scopes_raw = token_response.get("scope") or ""
        granted_scopes = [s for s in granted_scopes_raw.split() if s]
        tokens_for_profile = {
            "access_token": token_response.get("access_token"),
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": granted_scopes or provider.scopes_for_role(pending.role),
            "token_uri": provider.token_url,
        }
        profile = provider.profile_fetcher(tokens_for_profile)

        label = _role_label(provider, pending.role, profile)
        tokens = OAuthTokens(
            access_token=token_response.get("access_token"),
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            scopes=granted_scopes or provider.scopes_for_role(pending.role),
            token_uri=provider.token_url,
        )
        account = self._accounts.upsert(
            provider=pending.provider,
            role=pending.role,
            label=label,
            email=profile.email,
            username=profile.username,
            scopes=tokens.scopes,
            tokens=tokens,
            make_default=pending.make_default,
        )
        log.info(
            "oauth_complete",
            provider=pending.provider,
            role=pending.role,
            account_id=account.account_id,
            email=account.email,
        )
        return account

    # ── internals ─────────────────────────────────────────────────

    def _purge_expired_locked(self) -> None:
        now = time.time()
        for state in list(self._pending):
            if now - self._pending[state].created_at > STATE_TTL_SECONDS:
                self._pending.pop(state, None)


def _exchange_code(
    *,
    token_url: str,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict:
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
    try:
        return json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"token exchange returned non-JSON: {raw[:200]}") from e


def _role_label(provider: OAuthProvider, role: Role, profile) -> str:
    """Short human label used in the UI row."""
    role_word = "PILK" if role == "system" else "You"
    who = profile.email or profile.username or "account"
    return f"{provider.label} · {role_word} · {who}"
