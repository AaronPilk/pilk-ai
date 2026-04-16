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
    "claude-opus-4-7": ModelPrice(5.00, 25.00),
    "claude-opus-4-6": ModelPrice(5.00, 25.00),
    "claude-sonnet-4-6": ModelPrice(3.00, 15.00),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00),
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
