"""System-level carve-outs.

Tools that materially change PILK itself — registering agents, editing
policy rules, rotating credentials — always require a fresh per-call
approval. Even if the base risk class would auto-allow (WRITE_LOCAL in
workspace), modifying the system is something the user must consent to
each time, and the approval cannot be covered by a trust rule.

Add new tools to `ALWAYS_REQUIRES_APPROVAL` as the system grows.
"""

from __future__ import annotations

from dataclasses import dataclass

ALWAYS_REQUIRES_APPROVAL: frozenset[str] = frozenset({"agent_create"})


@dataclass(frozen=True)
class SystemRuling:
    requires_approval: bool = False
    bypass_trust: bool = False
    reason: str = ""


def evaluate(*, tool_name: str) -> SystemRuling:
    if tool_name in ALWAYS_REQUIRES_APPROVAL:
        return SystemRuling(
            requires_approval=True,
            bypass_trust=True,
            reason=f"{tool_name}: system change — each call requires fresh approval",
        )
    return SystemRuling()
