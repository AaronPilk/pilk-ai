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
    NET_WRITE = "NET_WRITE"
    COMMS = "COMMS"
    FINANCIAL = "FINANCIAL"
    IRREVERSIBLE = "IRREVERSIBLE"
