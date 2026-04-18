"""Structured logging for pilkd.

Single JSON-lines log on stderr plus a mirrored file under `~/PILK/logs/`.
Every log record carries a `source` field so the dashboard can filter.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog


def configure_logging(level: str, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pilkd.log"

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.WriteLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    file_logger = logging.getLogger("pilkd.file")
    file_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    file_logger.addHandler(handler)


def get_logger(source: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger().bind(source=source)
