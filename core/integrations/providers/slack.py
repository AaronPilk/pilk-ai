"""Slack as an OAuthProvider.

Slack's OAuth is shaped a bit differently from Google's:

- User tokens live at `authed_user.access_token` in the token-exchange
  response, not at the top level — the provider's `token_extractor`
  pulls them out.
- The auth URL uses `user_scope=` (not `scope=`) for user tokens, so
  `scope_param_name="user_scope"`.
- Access tokens don't expire by default — there's no refresh_token
  unless the user enables token rotation in their Slack app. We mark
  `requires_refresh_token=False` and store the access token as the
  long-lived credential.

User role only in this batch. Bot-role Slack (PILK posting as itself)
needs a different scope surface and a different token extraction path;
deferred.
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

AUTH_URL = "https://slack.com/oauth/v2/authorize"
TOKEN_URL = "https://slack.com/api/oauth.v2.access"
AUTH_TEST_URL = "https://slack.com/api/auth.test"

SCOPE_CATALOG: dict[str, ScopeSpec] = {
    "chat.write": ScopeSpec(
        name="chat.write",
        scope_uri="chat:write",
        label="Post messages as you",
        risk_hint=RiskClass.COMMS,
        user_voice=True,
    ),
    "channels.read": ScopeSpec(
        name="channels.read",
        scope_uri="channels:read",
        label="List public channels",
        risk_hint=RiskClass.NET_READ,
    ),
}

_GROUP_SCOPES: dict[str, list[str]] = {
    "messages": ["chat.write"],
    "channels": ["channels.read"],
}

SCOPE_GROUP_LABELS: dict[str, str] = {
    "messages": "Send messages",
    "channels": "List channels",
}


def _scopes_for_role(role: Role, groups: list[str] | None = None) -> list[str]:
    # System role isn't supported in this batch; just be defensive.
    if role != "user":
        return []
    active = [g for g in (groups or ("messages",)) if g]
    names: set[str] = set()
    for g in active:
        names.update(_GROUP_SCOPES.get(g, []))
    return [SCOPE_CATALOG[n].scope_uri for n in sorted(names)]


def _token_extractor(raw: dict) -> dict:
    """Pull the user token out of Slack's nested response."""
    if not raw.get("ok", True):
        raise RuntimeError(f"slack oauth error: {raw.get('error', 'unknown')}")
    authed = raw.get("authed_user") or {}
    user_token = authed.get("access_token")
    scope = authed.get("scope", raw.get("scope", ""))
    # Slack doesn't return a refresh_token by default; leave it empty
    # and rely on the provider's requires_refresh_token=False to let the
    # flow complete.
    return {
        "access_token": user_token,
        "refresh_token": "",
        "scope": scope,
        "token_type": authed.get("token_type") or raw.get("token_type"),
        "team": raw.get("team"),
        "authed_user_id": authed.get("id"),
    }


def _fetch_profile(tokens: dict) -> OAuthProfile:
    """Call auth.test with the freshly-minted user token.

    Returns the authenticated user's name + team. Email isn't available
    without the identity scopes — for the row we use the synthetic
    `user@team` format as the email field so the UI keeps a stable
    identity line.
    """
    access_token = tokens.get("access_token")
    if not access_token:
        return OAuthProfile(email=None, username=None)
    try:
        req = urllib.request.Request(
            AUTH_TEST_URL,
            data=b"",
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
        info = json.loads(body)
    except Exception:
        return OAuthProfile(email=None, username=None)
    if not info.get("ok"):
        return OAuthProfile(email=None, username=None)
    user = info.get("user") or ""
    team = info.get("team") or ""
    synthetic_email = f"{user}@{team}" if user and team else None
    return OAuthProfile(
        email=synthetic_email,
        username=user or None,
        extra={
            "slack_user_id": info.get("user_id"),
            "slack_team_id": info.get("team_id"),
            "workspace_url": info.get("url"),
        },
    )


slack_provider = OAuthProvider(
    name="slack",
    label="Slack",
    auth_url=AUTH_URL,
    token_url=TOKEN_URL,
    scope_catalog=SCOPE_CATALOG,
    scopes_for_role=_scopes_for_role,
    profile_fetcher=_fetch_profile,
    scope_groups=dict(SCOPE_GROUP_LABELS),
    default_scope_groups=("messages",),
    supports_roles=("user",),
    extra_auth_params={},  # Slack doesn't accept access_type/prompt
    requires_refresh_token=False,
    scope_param_name="user_scope",
    token_extractor=_token_extractor,
)
