"""Self-capabilities refresh — PILK summarizes its own current
features into a brain note so it doesn't drift out of touch with
its own deployment.

Read by PILK on every conversation through standing-instructions.
Refreshed automatically at boot when the running code's git HEAD
differs from the hash recorded in the existing note. Manual
refresh is exposed at ``POST /system/refresh-capabilities``.

Costs ~5-10¢ per real deploy via one Anthropic call. Zero on
restarts where nothing shipped (HEAD already matches).
"""

from core.self_capabilities.refresher import (
    RefreshOutcome,
    SelfCapabilitiesRefresher,
)

__all__ = ["RefreshOutcome", "SelfCapabilitiesRefresher"]
