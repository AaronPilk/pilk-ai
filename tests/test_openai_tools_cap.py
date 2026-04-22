"""OpenAI tool-cap: registries bigger than OpenAI's 128 ceiling get
clamped with core primitives and provider-prefixed tools preserved
ahead of specialist surfaces.
"""

from __future__ import annotations

from core.governor.providers.openai_provider import (
    OPENAI_MAX_TOOLS,
    _prioritised_cap,
)


def _t(name: str) -> dict:
    return {"name": name, "description": "", "input_schema": {}}


def test_cap_noop_when_under_limit() -> None:
    tools = [_t(f"tool_{i}") for i in range(10)]
    assert _prioritised_cap(tools, 128) == tools


def test_cap_keeps_core_names_when_overflowing() -> None:
    filler = [_t(f"xauusd_placeholder_{i}") for i in range(200)]
    # Scatter a couple of core tools near the end so alphabetical /
    # insertion order would drop them if priority weren't honoured.
    core = [_t("fs_read"), _t("shell_exec"), _t("llm_ask"), _t("brain_search")]
    tools = filler + core
    kept = _prioritised_cap(tools, OPENAI_MAX_TOOLS)
    assert len(kept) == OPENAI_MAX_TOOLS
    kept_names = {t["name"] for t in kept}
    for required in ("fs_read", "shell_exec", "llm_ask", "brain_search"):
        assert required in kept_names


def test_cap_keeps_prefixed_tools_ahead_of_specialists() -> None:
    # 130 xauusd tools + 10 gmail tools. Gmail prefix should survive
    # the cut; xauusd should take the hit.
    xauusd = [_t(f"xauusd_{i}") for i in range(130)]
    gmail = [_t(f"gmail_tool_{i}") for i in range(10)]
    tools = xauusd + gmail
    kept = _prioritised_cap(tools, OPENAI_MAX_TOOLS)
    kept_names = {t["name"] for t in kept}
    for g in gmail:
        assert g["name"] in kept_names
    # Some xauusd tools survive but at least 12 are dropped.
    xauusd_dropped = sum(
        1 for x in xauusd if x["name"] not in kept_names
    )
    assert xauusd_dropped >= 12


def test_cap_preserves_caller_order_within_priority_bucket() -> None:
    # All tools share the same priority bucket (no core names / prefixes).
    tools = [_t(f"aaa_{i:03d}") for i in range(200)]
    kept = _prioritised_cap(tools, OPENAI_MAX_TOOLS)
    names = [t["name"] for t in kept]
    # First 128 in insertion order.
    assert names == [f"aaa_{i:03d}" for i in range(OPENAI_MAX_TOOLS)]
