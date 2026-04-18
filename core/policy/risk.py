"""Risk classes tagged on every tool at definition time.

The policy layer combines (risk x agent policy x sandbox policy x user
override) to decide whether a tool call runs, pauses for approval, or is
rejected. Batch 1 implements the risk tag and a minimal allow-list for the
low-risk classes; the full approval flow lands in batch 3.
"""

from __future__ import annotations

from enum import StrEnum


class RiskClass(StrEnum):
    READ = "READ"
    WRITE_LOCAL = "WRITE_LOCAL"
    EXEC_LOCAL = "EXEC_LOCAL"
    NET_READ = "NET_READ"
    # Browsing inside an isolated sandbox — navigation, typing into search
    # boxes and forms, scraping. Classified separately from NET_WRITE so
    # the gate can auto-allow it once the user has authorized a task.
    # Real outbound writes (email, posts, payments) stay on NET_WRITE /
    # COMMS / FINANCIAL.
    BROWSE = "BROWSE"
    NET_WRITE = "NET_WRITE"
    COMMS = "COMMS"
    FINANCIAL = "FINANCIAL"
    IRREVERSIBLE = "IRREVERSIBLE"
