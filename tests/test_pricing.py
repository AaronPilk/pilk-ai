from core.ledger.pricing import price_usage


def test_opus_47_basic_pricing() -> None:
    # 1M input tokens at $5 → $5.00. 1M output tokens at $25 → $25.00.
    assert price_usage(
        "claude-opus-4-7", input_tokens=1_000_000, output_tokens=0
    ) == 5.0
    assert price_usage(
        "claude-opus-4-7", input_tokens=0, output_tokens=1_000_000
    ) == 25.0


def test_cache_read_is_one_tenth() -> None:
    # Cache read is 0.1x input price.
    usd = price_usage(
        "claude-opus-4-7",
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=1_000_000,
    )
    assert usd == 0.5  # $5 * 0.1


def test_cache_write_premium() -> None:
    # Cache write (5-min TTL) is 1.25x input price.
    usd = price_usage(
        "claude-opus-4-7",
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=1_000_000,
    )
    assert usd == 6.25  # $5 * 1.25


def test_haiku_pricing_is_cheaper_than_opus() -> None:
    opus = price_usage("claude-opus-4-7", input_tokens=100_000, output_tokens=10_000)
    haiku = price_usage("claude-haiku-4-5", input_tokens=100_000, output_tokens=10_000)
    assert haiku < opus / 4


def test_unknown_model_returns_zero() -> None:
    assert price_usage("nonexistent-model", input_tokens=1000, output_tokens=500) == 0.0
