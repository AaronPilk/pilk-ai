"""File-backed client store.

Scans ``clients/*.yaml`` at boot (or on ``reload()``) and surfaces
:class:`Client` objects by slug. No SQLite mirror — the YAML files are
the source of truth, so editing them + redeploying / calling reload()
is the only way to change a client's config.

Files starting with ``_`` are ignored — that's how ``_example.yaml``
stays visible to humans without being loaded as a real client.

Invalid YAML / schema errors don't crash the daemon. They log a
warning keyed by filename and continue loading the rest. A client you
can't load still appears in the startup log as a problem, not as an
absence.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from core.clients.models import Client
from core.logging import get_logger

log = get_logger("pilkd.clients")


class ClientStore:
    """In-memory map of slug → :class:`Client`.

    The daemon constructs one of these in its FastAPI lifespan; tools
    that need client context read through :func:`get_client_store`.
    """

    def __init__(self, clients_dir: Path) -> None:
        self._dir = clients_dir
        self._by_slug: dict[str, Client] = {}

    # ── Public API ──────────────────────────────────────────────

    def reload(self) -> tuple[int, int]:
        """Re-scan the directory. Returns ``(loaded, errors)``.

        Clients that fail validation are logged + skipped, not raised
        — a single bad YAML file shouldn't take the daemon down."""
        loaded = 0
        errors = 0
        new_map: dict[str, Client] = {}

        if not self._dir.exists():
            log.info("clients_dir_absent", path=str(self._dir))
            self._by_slug = new_map
            return (0, 0)

        for path in sorted(self._dir.glob("*.yaml")):
            if path.name.startswith("_"):
                # _example.yaml and friends: visible on disk, not loaded.
                continue
            try:
                client = _read_client(path)
            except (OSError, yaml.YAMLError, ValidationError) as e:
                errors += 1
                log.warning(
                    "client_load_failed",
                    path=str(path),
                    error=str(e)[:300],
                )
                continue
            if client.slug in new_map:
                errors += 1
                log.warning(
                    "client_slug_collision",
                    slug=client.slug,
                    path=str(path),
                )
                continue
            new_map[client.slug] = client
            loaded += 1

        self._by_slug = new_map
        log.info("clients_loaded", loaded=loaded, errors=errors)
        return (loaded, errors)

    def get(self, slug: str) -> Client | None:
        return self._by_slug.get(slug)

    def list(self) -> list[Client]:
        return sorted(self._by_slug.values(), key=lambda c: c.slug)

    def slugs(self) -> Iterable[str]:
        return list(self._by_slug)


# ── Module-wide singleton ────────────────────────────────────────

_store: ClientStore | None = None


def set_client_store(store: ClientStore | None) -> None:
    global _store
    _store = store


def get_client_store() -> ClientStore | None:
    return _store


# ── Internal helpers ─────────────────────────────────────────────


def _read_client(path: Path) -> Client:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValidationError.from_exception_data(
            title="Client",
            line_errors=[],
        )
    # Fall back to the filename stem when the YAML omits ``slug`` so
    # simple fixtures work without boilerplate. An explicit ``slug``
    # field always wins.
    if "slug" not in data:
        data["slug"] = path.stem
    return Client.model_validate(data)


__all__ = [
    "ClientStore",
    "get_client_store",
    "set_client_store",
]
