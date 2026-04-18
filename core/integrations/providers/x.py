"""X (formerly Twitter) as an OAuthProvider.

X's OAuth 2.0 has two quirks PILK's framework already supports:

- PKCE is mandatory (`uses_pkce=True`). The flow generates a
  code_verifier per auth attempt and sends a SHA-256 challenge.
- Token exchange uses HTTP Basic auth instead of form-body creds
  (`token_exchange_mode="basic"`).

`offline.access` is included so we get a refresh_token — posting is a
COMMS-risk action the user shouldn't re-link for every couple hours.
`requires_refresh_token=True` is therefore safe here.

User role only. Posting is via `POST https://api.twitter.com/2/tweets`.
Reading your timeline, media uploads, threads — all deferred.
"""

from __future__ import annotations

import json
import urllib.request

from core.integrations.provider import (
    OAuthProfile,
    OAuthProvider,
    Role,
    ScopeSpec,
)
from core.policy.risk import RiskClass

AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
USERS_ME_URL = "https://api.twitter.com/2/users/me"

SCOPE_CATALOG: dict[str, ScopeSpec] = {
    "tweet.read": ScopeSpec(
        name="tweet.read",
        scope_uri="tweet.read",
        label="Read public tweets",
        risk_hint=RiskClass.NET_READ,
    ),
    "tweet.write": ScopeSpec(
        name="tweet.write",
        scope_uri="tweet.write",
        label="Post on X as you",
        risk_hint=RiskClass.COMMS,
        user_voice=True,
    ),
    "users.read": ScopeSpec(
        name="users.read",
        scope_uri="users.read",
        label="Your X profile",
        risk_hint=RiskClass.READ,
    ),
    "offline.access": ScopeSpec(
        name="offline.access",
        scope_uri="offline.access",
        label="Stay signed in (refresh token)",
        risk_hint=RiskClass.READ,
    ),
}

_GROUP_SCOPES: dict[str, list[str]] = {
    "posts": ["tweet.read", "tweet.write", "users.read", "offline.access"],
}

SCOPE_GROUP_LABELS: dict[str, str] = {
    "posts": "Post as you",
}


def _scopes_for_role(role: Role, groups: list[str] | None = None) -> list[str]:
    if role != "user":
        return []
    active = [g for g in (groups or ("posts",)) if g]
    names: set[str] = set()
    for g in active:
        names.update(_GROUP_SCOPES.get(g, []))
    return [SCOPE_CATALOG[n].scope_uri for n in sorted(names)]


def _fetch_profile(tokens: dict) -> OAuthProfile:
    access_token = tokens.get("access_token")
    if not access_token:
        return OAuthProfile(email=None, username=None)
    try:
        req = urllib.request.Request(
            USERS_ME_URL,
            method="GET",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return OAuthProfile(email=None, username=None)
    info = data.get("data") or {}
    handle = info.get("username")
    name = info.get("name")
    # X doesn't expose email via v2; synthesize @handle as the identity
    # string so the UI row is meaningful.
    synthetic_email = f"@{handle}" if handle else None
    return OAuthProfile(
        email=synthetic_email,
        username=handle or name,
        extra={"x_user_id": info.get("id"), "display_name": name},
    )


x_provider = OAuthProvider(
    name="x",
    label="X",
    auth_url=AUTH_URL,
    token_url=TOKEN_URL,
    scope_catalog=SCOPE_CATALOG,
    scopes_for_role=_scopes_for_role,
    profile_fetcher=_fetch_profile,
    scope_groups=dict(SCOPE_GROUP_LABELS),
    default_scope_groups=("posts",),
    supports_roles=("user",),
    extra_auth_params={},
    requires_refresh_token=True,
    uses_pkce=True,
    token_exchange_mode="basic",
)
