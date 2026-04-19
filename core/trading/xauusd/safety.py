"""Cross-cutting safety helpers for the XAU/USD adapter layer.

Kept separate from the broker module so the check is unit-testable in
isolation and so tests don't need Playwright to exercise it.

The one job here is: **refuse any UI interaction whose visible label
matches a forbidden pattern** — deposit / withdraw / transfer / etc.
The adapter calls ``forbidden_label_error(label, pool)`` before every
click or fill; a truthy return is the refusal message the caller must
surface (never silently swallow).
"""

from __future__ import annotations

from collections.abc import Iterable


def check_forbidden_label(label: str, forbidden: Iterable[str]) -> str | None:
    """Return the matched forbidden word iff ``label`` contains it.

    Case-insensitive substring match — Hugosway uses mixed casing for
    e.g. "Deposit" in the top bar but we can't rely on that. ``None``
    when the label is clean.
    """
    lowered = label.casefold().strip()
    if not lowered:
        return None
    for pattern in forbidden:
        if pattern.casefold().strip() in lowered:
            return pattern
    return None


def forbidden_label_error(
    label: str, forbidden: Iterable[str]
) -> str | None:
    """Return a formatted refusal message, or ``None`` if the label is
    safe to act on."""
    match = check_forbidden_label(label, forbidden)
    if match is None:
        return None
    return (
        f"refused: '{label}' contains forbidden keyword '{match}'. "
        "The XAUUSD agent is not allowed to click/fill any UI element "
        "related to deposits, withdrawals, or account funding."
    )


__all__ = ["check_forbidden_label", "forbidden_label_error"]
