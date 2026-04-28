"""Per-model pricing and per-call USD math.

Prices are USD per million tokens. Cache reads are billed at 0.1x input;
cache writes (5-min TTL) at 1.25x input. If Anthropic changes a price,
update this table — it is the single source of truth the ledger reads.
"""

from __future__ import annotations

from dataclasses import dataclass

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float


MODEL_PRICING: dict[str, ModelPrice] = {
    # Anthropic — USD per million tokens.
    "claude-opus-4-7": ModelPrice(5.00, 25.00),
    "claude-opus-4-6": ModelPrice(5.00, 25.00),
    "claude-sonnet-4-6": ModelPrice(3.00, 15.00),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00),
    # Subscription path — Claude Code CLI returns short aliases ("sonnet",
    # "haiku") instead of versioned names. The cost is $0 for these
    # because they're fixed-fee subscription, not per-call API. We map
    # them to zero pricing so they don't fail the lookup and end up
    # untagged.
    "sonnet": ModelPrice(0.00, 0.00),
    "haiku": ModelPrice(0.00, 0.00),
    "opus": ModelPrice(0.00, 0.00),

    # OpenAI — USD per million tokens. Source: openai.com/api/pricing
    # (snapshot 2026-04). Update when OpenAI changes them. Without
    # these entries every OpenAI call records as $0 and the dashboard
    # under-reports spend by however much GPT-4o has been used.
    "gpt-4o": ModelPrice(2.50, 10.00),
    "gpt-4o-mini": ModelPrice(0.15, 0.60),
    "gpt-4o-2024-08-06": ModelPrice(2.50, 10.00),
    "gpt-4o-2024-11-20": ModelPrice(2.50, 10.00),
    "gpt-4-turbo": ModelPrice(10.00, 30.00),
    "gpt-4-turbo-2024-04-09": ModelPrice(10.00, 30.00),
    "gpt-4": ModelPrice(30.00, 60.00),
    "gpt-3.5-turbo": ModelPrice(0.50, 1.50),
    "gpt-5.1": ModelPrice(2.00, 8.00),
    "gpt-5.2": ModelPrice(2.00, 8.00),
    # Embeddings — input-only models. Output_per_mtok=0 since the API
    # returns vectors, not text tokens.
    "text-embedding-3-small": ModelPrice(0.02, 0.0),
    "text-embedding-3-large": ModelPrice(0.13, 0.0),
    "text-embedding-ada-002": ModelPrice(0.10, 0.0),
}


def price_usage(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    """USD cost for a single `messages.create` call, inclusive of cache tokens."""
    price = MODEL_PRICING.get(model)
    if price is None:
        return 0.0
    per = 1_000_000
    total = (
        input_tokens * price.input_per_mtok / per
        + output_tokens * price.output_per_mtok / per
        + cache_creation_input_tokens
        * price.input_per_mtok
        * CACHE_WRITE_MULTIPLIER
        / per
        + cache_read_input_tokens
        * price.input_per_mtok
        * CACHE_READ_MULTIPLIER
        / per
    )
    return round(total, 6)
