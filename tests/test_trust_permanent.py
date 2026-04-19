"""TrustStore permanent-rule + predicate tests.

Ensures the existing (TTL, exact-args) contract survives and the new
escape hatches behave as documented.
"""

from __future__ import annotations

import time

import pytest

from core.policy.trust import TrustStore


def test_add_ttl_rule_round_trips() -> None:
    store = TrustStore()
    rule = store.add(
        agent_name="agent_a",
        tool_name="fs_read",
        ttl_seconds=60,
        reason="test",
    )
    assert rule.agent_name == "agent_a"
    assert not rule.permanent
    # TTL encoded in expires_at.
    assert rule.expires_at > time.time()


def test_add_rejects_negative_ttl() -> None:
    store = TrustStore()
    with pytest.raises(ValueError, match="ttl_seconds"):
        store.add(agent_name=None, tool_name="x", ttl_seconds=0)


def test_add_requires_ttl_or_permanent() -> None:
    store = TrustStore()
    with pytest.raises(ValueError, match="ttl_seconds"):
        store.add(agent_name=None, tool_name="x")


def test_add_permanent_ignores_ttl_field() -> None:
    store = TrustStore()
    with pytest.raises(ValueError, match="permanent"):
        store.add(
            agent_name=None,
            tool_name="x",
            ttl_seconds=60,
            permanent=True,
        )


def test_permanent_rule_never_expires() -> None:
    store = TrustStore()
    rule = store.add(
        agent_name=None,
        tool_name="x",
        permanent=True,
        reason="forever",
    )
    assert rule.permanent
    # Pretend a year has passed.
    assert not rule.is_expired(now=time.time() + 365 * 86400)


def test_ttl_rule_expires_at_boundary() -> None:
    store = TrustStore()
    rule = store.add(agent_name=None, tool_name="x", ttl_seconds=1)
    # Just past expiry.
    assert rule.is_expired(now=rule.expires_at + 0.01)


def test_match_respects_agent_scoping() -> None:
    store = TrustStore()
    store.add(
        agent_name="agent_a",
        tool_name="x",
        ttl_seconds=60,
    )
    # Different agent → no match.
    assert (
        store.match(agent_name="agent_b", tool_name="x", args={}) is None
    )
    # Same agent → match.
    assert (
        store.match(agent_name="agent_a", tool_name="x", args={})
        is not None
    )


def test_match_exact_args_subset_semantics() -> None:
    store = TrustStore()
    store.add(
        agent_name=None,
        tool_name="fs_read",
        args_matcher={"path": "/etc/hosts"},
        ttl_seconds=60,
    )
    # Matching path + extra args → match.
    assert (
        store.match(
            agent_name=None,
            tool_name="fs_read",
            args={"path": "/etc/hosts", "max_bytes": 100},
        )
        is not None
    )
    # Different path → no match.
    assert (
        store.match(
            agent_name=None,
            tool_name="fs_read",
            args={"path": "/etc/passwd"},
        )
        is None
    )


def test_predicate_blocks_match_when_false() -> None:
    store = TrustStore()
    store.add(
        agent_name=None,
        tool_name="x",
        permanent=True,
        predicate=lambda args: args.get("ok") is True,
    )
    assert (
        store.match(agent_name=None, tool_name="x", args={"ok": True})
        is not None
    )
    assert (
        store.match(agent_name=None, tool_name="x", args={"ok": False})
        is None
    )


def test_predicate_composes_with_args_matcher() -> None:
    store = TrustStore()
    store.add(
        agent_name=None,
        tool_name="x",
        args_matcher={"a": 1},
        permanent=True,
        predicate=lambda args: args.get("b", 0) > 0,
    )
    # Both must pass.
    assert (
        store.match(agent_name=None, tool_name="x", args={"a": 1, "b": 5})
        is not None
    )
    assert (
        store.match(agent_name=None, tool_name="x", args={"a": 1, "b": 0})
        is None
    )
    assert (
        store.match(agent_name=None, tool_name="x", args={"a": 2, "b": 5})
        is None
    )


def test_purge_skips_permanent_rules() -> None:
    store = TrustStore()
    store.add(agent_name=None, tool_name="x", permanent=True)
    store.add(agent_name=None, tool_name="y", ttl_seconds=60)
    # Force a purge with an absurdly future 'now'.
    store._purge_expired()
    rules = store.list()
    assert any(r.tool_name == "x" for r in rules)


def test_public_dict_shape_permanent_vs_ttl() -> None:
    store = TrustStore()
    perm = store.add(
        agent_name=None, tool_name="a", permanent=True,
        predicate_label="test label",
    )
    ttl = store.add(agent_name=None, tool_name="b", ttl_seconds=60)
    pd_perm = perm.public_dict()
    pd_ttl = ttl.public_dict()
    assert pd_perm["permanent"] is True
    assert pd_perm["expires_at"] is None
    assert pd_perm["expires_in_s"] is None
    assert pd_perm["predicate_label"] == "test label"
    assert pd_ttl["permanent"] is False
    assert pd_ttl["expires_at"] is not None
    assert pd_ttl["expires_in_s"] >= 0


def test_revoke_works_for_permanent_rules() -> None:
    store = TrustStore()
    rule = store.add(agent_name=None, tool_name="x", permanent=True)
    assert store.revoke(rule.id) is True
    assert (
        store.match(agent_name=None, tool_name="x", args={}) is None
    )
