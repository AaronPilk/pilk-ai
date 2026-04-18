"""Communications sub-policy — hard constraints on outbound messages.

Sending email as *the user* is categorically different from sending as
PILK itself. An email that goes out from `aaron@…` is the user
publicly signing something; it should not be coverable by a trust
rule. Every single outgoing message from the user identity has to
land in the approval queue, with the recipient and body visible.

Sending from PILK's *system* identity (sentientpilkai@…) is still
COMMS and still hits the approval queue by default, but it *can* be
covered by narrow trust rules — e.g. "auto-approve report emails to
yourself", "auto-approve dev-signup verify replies". That pattern
matches how the financial sub-policy treats `trade_execute` inside a
trading sandbox.

Tool names here must match the ones produced by
`core.integrations.google.gmail.make_gmail_tools` for the user role.
"""

from __future__ import annotations

from dataclasses import dataclass

# Tools that always require fresh per-call approval and cannot be
# covered by a trust rule. Adding new user-identity outbound tools
# (LinkedIn, X, SMS, etc.) belongs here.
NEVER_WHITELISTABLE: frozenset[str] = frozenset(
    {
        "gmail_send_as_me",
        "slack_post_as_me",
    }
)


@dataclass(frozen=True)
class CommsRuling:
    bypass_trust: bool = False
    reason: str = ""


def evaluate(*, tool_name: str) -> CommsRuling:
    if tool_name in NEVER_WHITELISTABLE:
        return CommsRuling(
            bypass_trust=True,
            reason=(
                f"{tool_name}: sending as the user — every message requires "
                "fresh approval, no trust rules"
            ),
        )
    return CommsRuling()
