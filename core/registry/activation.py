"""Agent activation gating.

An agent is "active" when its manifest declares no external
dependencies, or when every declared dependency is configured (API
key present, OAuth linked). Pilk's catalog block and the ``/agents``
listing both use this module so there's one source of truth.

Deliberately conservative: we read config state only. Live probes
(a real authenticated call to the provider) live in
:mod:`core.registry.probes` once wired — this module already
accepts an optional probe lookup so integrating probe results is a
small follow-up rather than a rewrite.

### Why this exists

The operator can register 28 agents but only ~3 have their keys
entered. Without gating, Pilk's system prompt lists all 28, he tries
to delegate to one that can't run, the tool surfaces "X isn't
connected", the delegation fails. From the operator's seat it looks
like nothing works. Gating the catalog to active-only means the
blind-spot is visible: Pilk only references agents he can actually
drive, and the Agents UI surfaces the missing-setup chips on the
rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from core.registry.manifest import Manifest

# Forward-compatible set of statuses. Today we emit the first two;
# ``probe_failing`` is the slot probe-backed gating lands in.
AgentStatus = Literal["active", "needs_setup", "probe_failing"]


class SecretsLookup(Protocol):
    """Minimum surface of the integration-secrets store we need.

    Kept structural (Protocol) rather than importing the concrete
    store so this module stays cheap to import from the registry.
    """

    def get_value(self, key: str) -> str | None: ...


class AccountsLookup(Protocol):
    """Minimum surface of the OAuth accounts store we need."""

    def default(self, provider: str, role: str) -> Any: ...


@dataclass(frozen=True)
class MissingIntegration:
    """One unmet dependency on an agent manifest."""

    kind: Literal["api_key", "oauth"]
    name: str
    label: str
    role: str | None


@dataclass(frozen=True)
class ActivationReport:
    """Result of evaluating one manifest against the stores.

    ``status`` drives the catalog filter + UI chip. ``reason`` is the
    one-liner shown in tooltips + logs. ``missing`` is the full list
    of unmet dependencies for the UI to render individually.
    """

    status: AgentStatus
    reason: str
    missing: tuple[MissingIntegration, ...]

    def is_active(self) -> bool:
        return self.status == "active"


def evaluate(
    manifest: Manifest,
    *,
    secrets: SecretsLookup | None,
    accounts: AccountsLookup | None,
) -> ActivationReport:
    """Compute the activation state for one manifest.

    Rules:

    * No declared integrations → always ``active`` (local-only
      agents like ``file_organization_agent``).
    * At least one integration declared → must have ALL configured
      to be ``active``; any missing puts the agent in ``needs_setup``.
    * Probe results are not wired yet — every "configured"
      integration is trusted. Once probes land the status can
      transition to ``probe_failing`` without any signature change.

    The function is pure: no side effects, no IO beyond the two
    passed-in lookups. Safe to call on every ``/agents`` render.
    """
    if not manifest.integrations:
        return ActivationReport(
            status="active",
            reason="no external dependencies",
            missing=(),
        )
    missing: list[MissingIntegration] = []
    for spec in manifest.integrations:
        configured = False
        if spec.kind == "api_key" and secrets is not None:
            configured = secrets.get_value(spec.name) is not None
        elif spec.kind == "oauth" and accounts is not None:
            configured = (
                accounts.default(spec.name, spec.role or "user") is not None
            )
        if not configured:
            missing.append(
                MissingIntegration(
                    kind=spec.kind,
                    name=spec.name,
                    label=spec.label,
                    role=spec.role,
                )
            )
    if missing:
        summary = ", ".join(f"{m.kind}:{m.name}" for m in missing)
        return ActivationReport(
            status="needs_setup",
            reason=f"missing: {summary}",
            missing=tuple(missing),
        )
    return ActivationReport(
        status="active",
        reason="all declared integrations configured",
        missing=(),
    )


__all__ = [
    "AccountsLookup",
    "ActivationReport",
    "AgentStatus",
    "MissingIntegration",
    "SecretsLookup",
    "evaluate",
]
