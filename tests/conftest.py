from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.config import get_settings


@pytest.fixture(autouse=True)
def _isolated_pilk_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point PILK_HOME at a throwaway directory so tests never touch ~/PILK."""
    with tempfile.TemporaryDirectory(prefix="pilk-test-") as tmp:
        monkeypatch.setenv("PILK_HOME", tmp)
        get_settings.cache_clear()
        os.environ["PILK_HOME"] = tmp
        yield Path(tmp)
    get_settings.cache_clear()
