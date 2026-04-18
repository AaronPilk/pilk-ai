"""pilkd entrypoint.

Run with `python -m core.main` or `pilkd` (via the console script in
pyproject.toml). Uvicorn is launched programmatically so the daemon is a
first-class process rather than a CLI sub-invocation.
"""

from __future__ import annotations

import uvicorn

from core.api import create_app
from core.config import get_settings

app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "core.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
