import time

from core.policy.trust import TrustStore


def test_add_and_match_by_tool_and_args() -> None:
    store = TrustStore()
    store.add(
        agent_name="a1",
        tool_name="net_fetch",
        args_matcher={"url": "https://x.example"},
        ttl_seconds=60,
    )
    hit = store.match(
        agent_name="a1",
        tool_name="net_fetch",
        args={"url": "https://x.example", "timeout_s": 10},
    )
    assert hit is not None
    # Different agent → no match (wildcard uses None).
    assert (
        store.match(
            agent_name="a2",
            tool_name="net_fetch",
            args={"url": "https://x.example"},
        )
        is None
    )


def test_wildcard_agent_matches_any() -> None:
    store = TrustStore()
    store.add(
        agent_name=None,
        tool_name="net_fetch",
        args_matcher={},
        ttl_seconds=60,
    )
    hit = store.match(agent_name="anyone", tool_name="net_fetch", args={"url": "x"})
    assert hit is not None


def test_expired_rule_is_purged() -> None:
    store = TrustStore()
    rule = store.add(
        agent_name=None,
        tool_name="net_fetch",
        args_matcher={},
        ttl_seconds=1,
    )
    # Force-expire.
    rule.expires_at = time.time() - 5
    assert store.match(agent_name=None, tool_name="net_fetch", args={}) is None
    assert all(r.id != rule.id for r in store.list())


def test_revoke_removes_rule() -> None:
    store = TrustStore()
    rule = store.add(
        agent_name=None, tool_name="net_fetch", args_matcher={}, ttl_seconds=60
    )
    assert store.revoke(rule.id) is True
    assert store.revoke(rule.id) is False


def test_uses_counter_increments_on_match() -> None:
    store = TrustStore()
    rule = store.add(
        agent_name=None, tool_name="net_fetch", args_matcher={}, ttl_seconds=60
    )
    assert rule.uses == 0
    store.match(agent_name=None, tool_name="net_fetch", args={})
    store.match(agent_name=None, tool_name="net_fetch", args={})
    assert rule.uses == 2
