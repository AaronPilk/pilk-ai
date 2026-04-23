"""Router: goal → agent classifier.

Conservative by design. Only route when one agent clearly wins.
"""

from __future__ import annotations

from core.orchestrator.router import classify_agent
from core.registry.manifest import AgentPolicy, Manifest, SandboxSpec


def _mk(name: str, description: str) -> Manifest:
    return Manifest(
        name=name,
        description=description,
        system_prompt="you are a test agent.",
        tools=["fs_read"],
        sandbox=SandboxSpec(profile=name),
        policy=AgentPolicy(),
    )


def test_clear_match_routes():
    manifests = [
        _mk("meta_ads_agent", "Meta Ads operator: campaigns, ad sets, creatives, insights."),
        _mk("pitch_deck_agent", "Builds Google Slides decks for clients."),
    ]
    got = classify_agent("build me a pitch deck for skyway", manifests)
    assert got is not None
    assert got[0] == "pitch_deck_agent"


def test_conversational_falls_through_to_pilk():
    manifests = [
        _mk("meta_ads_agent", "Meta Ads operator."),
        _mk("sales_ops_agent", "Outbound sales."),
    ]
    for goal in ("hello", "how are you", "thanks", "what time is it"):
        assert classify_agent(goal, manifests) is None, f"should not route {goal!r}"


def test_ambiguous_goal_falls_through():
    """Two agents match equally — gap requirement keeps us safe."""
    manifests = [
        _mk("analysis_agent", "Analyze quarterly data."),
        _mk("reporting_agent", "Analyze monthly reports."),
    ]
    # Goal is exactly the overlap between the two agents — classifier
    # should stay its hand rather than pick arbitrarily.
    got = classify_agent("analyze", manifests)
    assert got is None


def test_sentinel_is_never_a_target():
    manifests = [
        _mk("sentinel", "Incident supervisor. Heartbeats and observability."),
    ]
    # Even a perfect lexical match should not route to sentinel.
    got = classify_agent("sentinel incident supervisor", manifests)
    assert got is None


def test_empty_manifests_returns_none():
    assert classify_agent("anything", []) is None


def test_empty_goal_returns_none():
    manifests = [_mk("meta_ads_agent", "Meta Ads operator.")]
    assert classify_agent("", manifests) is None
