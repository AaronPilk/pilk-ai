"""Per-client configuration store.

Read by the design agents (web_design_agent, pitch_deck_agent) when a
user names a client in a task. Wire the store into the FastAPI
lifespan once; tools reach it through :func:`get_client_store`.
"""

from core.clients.models import Client
from core.clients.store import (
    ClientStore,
    get_client_store,
    set_client_store,
)

__all__ = [
    "Client",
    "ClientStore",
    "get_client_store",
    "set_client_store",
]
