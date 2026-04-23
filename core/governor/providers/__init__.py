"""Provider registry for the Governor.

`build_providers` returns a `{name: PlannerProvider}` dict containing
only the providers for which credentials are configured. The orchestrator
looks up a provider by name for each plan turn; if the tier chosen by
the Governor points at an unavailable provider, the orchestrator falls
back to the Anthropic provider and logs the mismatch.
"""

from __future__ import annotations

import anthropic

from core.governor.providers.anthropic_provider import AnthropicPlannerProvider
from core.governor.providers.base import (
    PlannerProvider,
    PlannerResponse,
    TextBlock,
    ToolUseBlock,
    UsageLike,
)
from core.governor.providers.claude_code_provider import (
    ClaudeCodeBinaryMissingError,
    ClaudeCodeChatProvider,
)
from core.governor.providers.openai_provider import (
    GEMINI_BASE_URL,
    GROK_BASE_URL,
    OpenAIPlannerProvider,
)
from core.logging import get_logger

log = get_logger("pilkd.providers")

__all__ = [
    "AnthropicPlannerProvider",
    "ClaudeCodeBinaryMissingError",
    "ClaudeCodeChatProvider",
    "OpenAIPlannerProvider",
    "PlannerProvider",
    "PlannerResponse",
    "TextBlock",
    "ToolUseBlock",
    "UsageLike",
    "build_providers",
]


def build_providers(
    *,
    anthropic_client: anthropic.AsyncAnthropic | None,
    openai_api_key: str | None,
    gemini_api_key: str | None = None,
    grok_api_key: str | None = None,
    claude_code_binary: str | None = None,
    enable_claude_code_chat: bool = True,
) -> dict[str, PlannerProvider]:
    """Build the provider map consumed by the orchestrator.

    When ``enable_claude_code_chat`` is on (default) and the
    ``claude`` CLI is available, a ``claude_code`` provider is
    registered alongside ``anthropic``. The governor can route LIGHT-
    tier turns to it so the operator's Claude subscription covers
    routine chat instead of the API burning token credits.

    Gemini and Grok share the OpenAI provider's code path — both
    publish OpenAI-compatible Chat Completions endpoints, so we
    register one ``OpenAIPlannerProvider`` instance per available
    key with a different base URL and registry name. Tier config
    picks which one a tier routes to by name (``openai`` / ``gemini``
    / ``grok``).
    """
    providers: dict[str, PlannerProvider] = {}
    if anthropic_client is not None:
        providers["anthropic"] = AnthropicPlannerProvider(anthropic_client)
    if openai_api_key:
        providers["openai"] = OpenAIPlannerProvider(openai_api_key)
    if gemini_api_key:
        providers["gemini"] = OpenAIPlannerProvider(
            gemini_api_key, base_url=GEMINI_BASE_URL, name="gemini",
        )
    if grok_api_key:
        providers["grok"] = OpenAIPlannerProvider(
            grok_api_key, base_url=GROK_BASE_URL, name="grok",
        )
    if enable_claude_code_chat:
        try:
            providers["claude_code"] = ClaudeCodeChatProvider(
                binary=claude_code_binary or None,
            )
            log.info(
                "claude_code_chat_provider_registered",
                binary=claude_code_binary or "claude",
            )
        except ClaudeCodeBinaryMissingError as e:
            # Non-fatal: we just fall back to the Anthropic API path.
            # Log at info level so the operator sees it once at boot.
            log.info(
                "claude_code_chat_provider_skipped",
                reason=str(e),
            )
    return providers
