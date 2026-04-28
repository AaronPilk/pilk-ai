"""XAUUSD execution runner — the autonomous trading loop.

This package sits alongside the agent manifest in
``agents/xauusd_execution_agent/``. The manifest drives the orchestrator
path (declarative, LLM-driven each turn); this package drives the
imperative 5-minute-tick loop that keeps the machine running between
turns when the operator has started the agent from Telegram.
"""

from agents.xauusd_execution.runner import XAUUSDRunner, XAUUSDRunnerConfig

__all__ = ["XAUUSDRunner", "XAUUSDRunnerConfig"]
