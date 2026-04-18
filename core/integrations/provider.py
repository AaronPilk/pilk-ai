"""Provider-agnostic OAuth provider declarations.

Each integration (Google, Slack, LinkedIn, …) is one file in
`core.integrations.providers.*` that builds an `OAuthProvider`. The
generic OAuth driver + AccountsStore consume these — there's no
provider-specific code anywhere else.

Profile fetchers are sync callables (they usually wrap an HTTP call to
the provider's userinfo endpoint). The generic flow runs them via
`asyncio.to_thread` when needed so the event loop stays free.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from core.policy.risk import RiskClass

Role = Literal["system", "user"]


@dataclass(frozen=True)
class ScopeSpec:
    """One OAuth scope PILK understands.

    `name` is the short internal handle ("gmail.send"). `scope_uri` is
    the literal string the provider wants. `risk_hint` tags what kind
    of action this scope unlocks — the comms sub-policy uses it to
    auto-flag user-voice tools as never-trust.
    """

    name: str
    scope_uri: str
    label: str
    risk_hint: RiskClass
    user_voice: bool = False  # true for scopes that post publicly as the user


@dataclass
class OAuthProfile:
    """What profile_fetcher returns — minimal, provider-agnostic."""

    email: str | None
    username: str | None
    extra: dict = field(default_factory=dict)


@dataclass
class OAuthProvider:
    name: str                            # "google"
    label: str                           # "Google"
    auth_url: str                        # "https://accounts.google.com/o/oauth2/v2/auth"
    token_url: str                       # "https://oauth2.googleapis.com/token"
    scope_catalog: dict[str, ScopeSpec]  # keyed by ScopeSpec.name
    # role + optional scope-group list → list of scope URIs. Groups
    # let a user opt into wider access (e.g. "mail" + "drive") without
    # forcing every account to request every scope.
    scopes_for_role: Callable[[Role, list[str] | None], list[str]]
    profile_fetcher: Callable[[dict], OAuthProfile]  # tokens dict → profile
    # Named bundles of scopes the UI exposes as toggleable checkboxes.
    # Keys are group names; values are short human labels.
    scope_groups: dict[str, str] = field(default_factory=dict)
    # Groups requested when the UI doesn't pass any explicitly.
    default_scope_groups: tuple[str, ...] = ("mail",)
    supports_roles: tuple[Role, ...] = ("system", "user")
    extra_auth_params: dict = field(default_factory=lambda: {"access_type": "offline", "prompt": "consent"})
    # Google and most providers require a refresh_token for long-lived
    # background access. Slack doesn't — its access tokens are long-
    # lived by default. Providers that don't need one set this False.
    requires_refresh_token: bool = True
    # Google uses `scope=`; Slack user-mode uses `user_scope=`. Most
    # providers keep the default.
    scope_param_name: str = "scope"
    # Optional hook to normalize the raw token-exchange response into
    # the {access_token, refresh_token, scope, ...} shape the flow
    # expects. Slack nests user tokens under `authed_user`; Google
    # and most others just return them at the top level.
    token_extractor: Callable[[dict], dict] | None = None
    # PKCE (RFC 7636). X / Twitter requires it; everyone else is fine
    # without. When True the flow generates a code_verifier per auth
    # attempt and sends a SHA-256 challenge on the auth URL.
    uses_pkce: bool = False
    # "form" sends client_id + client_secret in the token-exchange
    # body (Google, Slack, LinkedIn). "basic" sends them in an HTTP
    # Authorization: Basic header (X / Twitter Confidential clients).
    token_exchange_mode: str = "form"


class ProviderRegistry:
    """Tiny map of provider name → OAuthProvider. No magic."""

    def __init__(self) -> None:
        self._providers: dict[str, OAuthProvider] = {}

    def register(self, provider: OAuthProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> OAuthProvider | None:
        return self._providers.get(name)

    def names(self) -> list[str]:
        return list(self._providers.keys())

    def all(self) -> list[OAuthProvider]:
        return list(self._providers.values())
