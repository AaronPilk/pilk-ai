"""Coding execution engines.

PILK is the orchestrator; a coding engine is one of several pluggable
backends a `code_task` can route to. Three engines today:

- `APIEngine`           — bare Anthropic Messages API. Always available
                          when the daemon has an API key. Good for
                          function/file-scope snippets.
- `ClaudeCodeBridge`    — local Claude Code session (scaffold; returns
                          unavailable until the bridge protocol lands).
                          Best for repo-scope work.
- `AgentSDKEngine`      — Anthropic Agent SDK inside a PILK sandbox
                          (scaffold; returns unavailable until wired).
                          Intermediate fallback for repo work.

All engines implement the `CodingEngine` protocol in `base.py`. The
`CodingRouter` picks one at runtime based on task scope, engine health,
user override, and the Governor's budget state.

Billing pools are deliberately separate:
- Claude Code runs record `cost_kind="claude-code"` with `usd=0.0`
  (flat subscription, no per-call USD attribution).
- Agent SDK and API engines record `cost_kind="llm"` with real USD
  via the existing Ledger — same pool as the orchestrator's planner.
"""

from core.coding.agent_sdk import AgentSDKEngine
from core.coding.api_engine import APIEngine
from core.coding.base import (
    CodeRunResult,
    CodeScope,
    CodeTask,
    CodingEngine,
    EngineHealth,
)
from core.coding.claude_code_bridge import ClaudeCodeBridge
from core.coding.codex_bridge import CodexBridge
from core.coding.router import CodingRouter

__all__ = [
    "APIEngine",
    "AgentSDKEngine",
    "ClaudeCodeBridge",
    "CodeRunResult",
    "CodeScope",
    "CodeTask",
    "CodexBridge",
    "CodingEngine",
    "CodingRouter",
    "EngineHealth",
]
