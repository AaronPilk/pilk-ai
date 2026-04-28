"""Batch 4A — tool-required routing guard.

Verifies the post-tier-choice routing logic that prevents master
agent runs (which need PILK tools) from landing on the Claude Code
CLI subprocess provider — that path doesn't bridge the PILK tool
registry through, so any tool call would silently fail.

Two surfaces under test:

1. ``Orchestrator._run_requires_tool_capable_provider`` — the boolean
   guard that decides whether a given ``RunContext`` MUST be routed
   to a tool-capable provider (Anthropic API today). Pure unit test.

2. The Master Reporting manifest — its tool list must include the
   read-only intelligence digest tool, and the manifest must declare
   a non-empty tool allowlist so the guard fires for it.

Plus a sanity check that top-level Pilk chat (``agent_name=None``)
is NOT affected by the guard — the cheap Claude Code path stays
available for simple no-tool conversational turns.
"""

from __future__ import annotations

from pathlib import Path

from core.orchestrator.orchestrator import Orchestrator, RunContext


# ── Helpers ────────────────────────────────────────────────────────


def _make_rc(
    *,
    agent_name: str | None,
    allowed_tools: list[str] | None,
    suppress: bool = False,
    goal: str = "do the thing",
) -> RunContext:
    """Construct a minimal RunContext just sufficient to exercise
    the routing guard. Only the fields the guard reads matter."""
    return RunContext(
        goal=goal,
        system_prompt="you are a test agent",
        allowed_tools=allowed_tools,
        agent_name=agent_name,
        sandbox_id=None,
        sandbox_root=None,
        sandbox_capabilities=frozenset(),
        metadata={},
        suppress_tool_capable_force=suppress,
    )


# ── 1. Boolean guard — top-level Pilk chat ─────────────────────────


def test_top_level_pilk_chat_does_not_force_tool_capable() -> None:
    """When Pilk runs as the planner (agent_name=None,
    allowed_tools=None ⇒ all tools allowed), the Batch 4A guard does
    NOT fire — the existing content-heuristic check still covers
    Pilk based on goal text. This preserves cheap Claude Code routing
    for plain conversational chat."""
    rc = _make_rc(agent_name=None, allowed_tools=None)
    assert Orchestrator._run_requires_tool_capable_provider(rc) is False


def test_top_level_pilk_chat_with_empty_tool_list_does_not_fire() -> None:
    """Even if a future caller passes an explicit empty list at the
    top level, the guard stays inert (no agent name)."""
    rc = _make_rc(agent_name=None, allowed_tools=[])
    assert Orchestrator._run_requires_tool_capable_provider(rc) is False


# ── 2. Boolean guard — agent runs ──────────────────────────────────


def test_agent_run_with_tools_forces_tool_capable() -> None:
    """A delegated master agent with a non-empty tool allowlist must
    be routed to a tool-capable provider — the Claude Code CLI
    subprocess can't bridge PILK tools through."""
    rc = _make_rc(
        agent_name="master_reporting",
        allowed_tools=["intelligence_digest_read", "fs_read"],
    )
    assert Orchestrator._run_requires_tool_capable_provider(rc) is True


def test_agent_run_with_empty_tool_list_does_not_fire() -> None:
    """A no-tool agent (e.g. a pure planner persona) doesn't need a
    tool-capable provider. The guard stays inert so tier choice is
    free to pick the cheap subscription path."""
    rc = _make_rc(agent_name="some_agent", allowed_tools=[])
    assert Orchestrator._run_requires_tool_capable_provider(rc) is False


def test_agent_run_with_none_tool_list_does_not_fire() -> None:
    """``allowed_tools=None`` means 'all registered tools available'
    — that's how Pilk's runtime context is built. The guard treats
    this as 'no explicit tool allowlist'; agent runs always supply
    a list, never None."""
    rc = _make_rc(agent_name="some_agent", allowed_tools=None)
    assert Orchestrator._run_requires_tool_capable_provider(rc) is False


# ── 3. Suppress flag escape hatch ──────────────────────────────────


def test_suppress_flag_disables_the_guard() -> None:
    """``suppress_tool_capable_force=True`` exists for callers that
    explicitly want CLI behavior (tests, programmatic invocations).
    The Batch 4A guard respects it the same way the existing
    content-heuristic check does."""
    rc = _make_rc(
        agent_name="master_reporting",
        allowed_tools=["intelligence_digest_read"],
        suppress=True,
    )
    assert Orchestrator._run_requires_tool_capable_provider(rc) is False


# ── 4. Master Reporting wiring sanity ──────────────────────────────


def test_master_reporting_manifest_has_non_empty_tool_list() -> None:
    """Master Reporting MUST declare a non-empty tool allowlist —
    otherwise the Batch 4A guard wouldn't fire for it and the agent
    would silently lose access to PILK tools on Claude Code routing.
    """
    from core.registry.manifest import Manifest

    m = Manifest.load(
        Path(__file__).resolve().parents[1]
        / "agents" / "master_reporting" / "manifest.yaml"
    )
    assert m.tools, (
        "Master Reporting manifest declares zero tools — Batch 4A "
        "guard would not fire and PILK tools would silently drop "
        "on Claude Code routing."
    )
    assert "intelligence_digest_read" in m.tools


def test_master_reporting_intel_brief_run_would_force_tool_capable() -> None:
    """End-to-end logical check: build a RunContext the way
    ``Orchestrator.agent_run`` would for a Master Reporting intel
    brief request, and assert the Batch 4A guard fires."""
    from core.registry.manifest import Manifest

    m = Manifest.load(
        Path(__file__).resolve().parents[1]
        / "agents" / "master_reporting" / "manifest.yaml"
    )
    rc = _make_rc(
        agent_name=m.name,
        allowed_tools=list(m.tools),
        goal="Give me today's intel brief.",
    )
    assert Orchestrator._run_requires_tool_capable_provider(rc) is True


# ── 5. Provider override metadata shape — call-site invariant ─────


def test_call_site_writes_required_metadata_fields() -> None:
    """Sanity-check that the call-site in ``_execute`` uses the keys
    the smoke-test surface expects. We assert the literal field
    names appear in the orchestrator source so a future refactor
    doesn't silently drop them — the smoke test report keys off
    these fields.
    """
    src = (
        Path(__file__).resolve().parents[1]
        / "core" / "orchestrator" / "orchestrator.py"
    ).read_text()
    # Reason suffix the smoke-test surface greps for.
    assert "+tool_required" in src
    # Boolean flag persisted into tier_meta (visible in the plan's
    # llm step output).
    assert 'tier_meta["tool_required"] = True' in src
    # Original (rejected) provider preserved so the operator can see
    # what the governor wanted before the override.
    assert 'tier_meta["original_provider"] = original_provider' in src
    # Structured log line so the daemon log can be grepped.
    assert "provider_override_tool_required" in src


def test_call_site_clear_error_for_agent_without_provider() -> None:
    """When neither the requested nor the fallback provider is
    available AND the run is for a tool-needing agent, the
    orchestrator must raise an error that names the agent + tool
    count instead of the generic 'no planner provider' message —
    the operator needs to know exactly what failed."""
    src = (
        Path(__file__).resolve().parents[1]
        / "core" / "orchestrator" / "orchestrator.py"
    ).read_text()
    assert (
        "requires PILK tools" in src
        and "declared in manifest" in src
        and "Set ANTHROPIC_API_KEY" in src
    )
