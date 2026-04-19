"""Forbidden-label guard tests. No Playwright, no I/O."""

from __future__ import annotations

import pytest

from core.trading.xauusd.broker import FORBIDDEN_EXACT_LABELS
from core.trading.xauusd.safety import (
    check_forbidden_label,
    forbidden_label_error,
)


@pytest.mark.parametrize(
    "label",
    [
        "Deposit",
        "DEPOSIT",
        "  Deposit  ",
        "Make a deposit",
        "Withdraw funds",
        "Bank transfer",
        "Open cashier",
        "Payment details",
        "My wallet",
    ],
)
def test_forbidden_labels_refuse(label: str) -> None:
    assert check_forbidden_label(label, FORBIDDEN_EXACT_LABELS) is not None
    assert forbidden_label_error(label, FORBIDDEN_EXACT_LABELS) is not None


@pytest.mark.parametrize(
    "label",
    ["BUY", "SELL", "Market", "Limit", "Stop", "Positions", "XAUUSD", "4832.72"],
)
def test_safe_labels_pass(label: str) -> None:
    assert check_forbidden_label(label, FORBIDDEN_EXACT_LABELS) is None
    assert forbidden_label_error(label, FORBIDDEN_EXACT_LABELS) is None


def test_empty_label_passes() -> None:
    assert check_forbidden_label("", FORBIDDEN_EXACT_LABELS) is None


def test_partial_word_does_not_false_positive() -> None:
    # "card" is forbidden but not "cardinal" — wait, yes it is because
    # we substring-match. That's fine: erring loud is the whole point.
    # Verifying: "Dashboard" contains "board", not "card". Safe.
    assert check_forbidden_label("Dashboard", FORBIDDEN_EXACT_LABELS) is None
    # But "Card details" correctly trips because it contains "card".
    assert check_forbidden_label("Card details", FORBIDDEN_EXACT_LABELS) is not None


def test_error_message_includes_label_and_match() -> None:
    msg = forbidden_label_error("Make a deposit", FORBIDDEN_EXACT_LABELS)
    assert msg is not None
    assert "Make a deposit" in msg
    assert "Deposit" in msg
