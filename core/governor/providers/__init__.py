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
from core.governor.providers.openai_provider import OpenAIPlannerProvider

__all__ = [
    "AnthropicPlannerProvider",
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
) -> dict[str, PlannerProvider]:
    providers: dict[str, PlannerProvider] = {}
    if anthropic_client is not None:
        providers["anthropic"] = AnthropicPlannerProvider(anthropic_client)
    if openai_api_key:
        providers["openai"] = OpenAIPlannerProvider(openai_api_key)
    return providers
