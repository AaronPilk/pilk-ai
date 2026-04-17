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
    scopes_for_role: Callable[[Role], list[str]]  # returns scope URIs
    profile_fetcher: Callable[[dict], OAuthProfile]  # tokens dict → profile
    supports_roles: tuple[Role, ...] = ("system", "user")
    extra_auth_params: dict = field(default_factory=lambda: {"access_type": "offline", "prompt": "consent"})


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
