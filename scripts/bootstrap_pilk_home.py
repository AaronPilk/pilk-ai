"""Create `~/PILK/` and initialize the database.

Run with `python -m scripts.bootstrap_pilk_home`. Idempotent.
"""

from __future__ import annotations

import sys

from core.config import get_settings
from core.db import ensure_schema

SUBDIRS = [
    "config",
    "logs",
    "memory",
    "sandboxes",
    "agents",
    "workspace",
    "exports",
    "temp",
]


def main() -> int:
    settings = get_settings()
    home = settings.resolve_home()
    home.mkdir(parents=True, exist_ok=True)

    for sub in SUBDIRS:
        (home / sub).mkdir(parents=True, exist_ok=True)

    user_config = home / "config" / "pilk.toml"
    if not user_config.exists():
        user_config.write_text(
            "# PILK user config. Overrides are applied on top of defaults.\n"
            "# This file is safe to edit.\n",
            encoding="utf-8",
        )

    ensure_schema(settings.db_path)

    print(f"PILK home ready at: {home}")
    print(f"Database:          {settings.db_path}")
    for sub in SUBDIRS:
        print(f"  {sub}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
