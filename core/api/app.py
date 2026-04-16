"""FastAPI app factory for pilkd.

Batch 0 surface: `/health`, `/version`, and `/ws` (echo). The dashboard
connects to `/ws` and exchanges JSON envelopes:

    {"type": "chat.user",  "id": "...", "text": "..."}
    {"type": "chat.reply", "id": "...", "text": "..."}

Later batches add more message types on the same socket (plan.updated,
step.progress, approval.requested, cost.updated, ...).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core import __version__
from core.api.routes.health import router as health_router
from core.api.ws import router as ws_router
from core.config import get_settings
from core.db import ensure_schema
from core.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    home = settings.resolve_home()
    configure_logging(settings.log_level, settings.logs_dir)
    log = get_logger("pilkd.startup")

    home.mkdir(parents=True, exist_ok=True)
    ensure_schema(settings.db_path)

    log.info("pilkd_ready", home=str(home), host=settings.host, port=settings.port)
    yield
    log.info("pilkd_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="pilkd",
        version=__version__,
        lifespan=lifespan,
    )

    # Dashboard runs on a different port in dev; loopback-only in both.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:1420",
            "http://localhost:1420",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(ws_router)
    return app
