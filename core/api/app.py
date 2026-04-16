"""FastAPI app factory for pilkd.

Lifespan wires up the shared singletons: SQLite schema, the connection
hub, the cost ledger, the tool registry, the gateway, the plan store,
the agent registry, the sandbox manager, and (if an API key is present)
the Anthropic client + orchestrator.

If ANTHROPIC_API_KEY is not set, pilkd still boots — the dashboard can
still render and the agent/sandbox tabs still hydrate, but chat and
agent.run return a friendly error until a key is set.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core import __version__
from core.api.hub import Hub
from core.api.routes.agents import router as agents_router
from core.api.routes.cost import router as cost_router
from core.api.routes.health import router as health_router
from core.api.routes.plans import router as plans_router
from core.api.routes.sandboxes import router as sandboxes_router
from core.api.ws import router as ws_router
from core.config import get_settings
from core.db import ensure_schema
from core.ledger import Ledger
from core.logging import configure_logging, get_logger
from core.orchestrator import Orchestrator, PlanStore
from core.policy import Gate
from core.registry import AgentRegistry
from core.sandbox import SandboxManager
from core.tools import Gateway, ToolRegistry
from core.tools.builtin import fs_read_tool, fs_write_tool, make_llm_ask_tool, shell_exec_tool

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "agents"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    home = settings.resolve_home()
    configure_logging(settings.log_level, settings.logs_dir)
    log = get_logger("pilkd.startup")

    home.mkdir(parents=True, exist_ok=True)
    ensure_schema(settings.db_path)

    hub = Hub()
    ledger = Ledger(settings.db_path)
    plans = PlanStore(settings.db_path)
    registry = ToolRegistry()
    registry.register(fs_read_tool)
    registry.register(fs_write_tool)
    registry.register(shell_exec_tool)
    gateway = Gateway(registry, Gate())

    agents = AgentRegistry(manifests_dir=AGENTS_DIR, db_path=settings.db_path)
    installed = await agents.discover_and_install()
    log.info("agents_discovered", names=installed, count=len(installed))

    sandboxes = SandboxManager(
        sandboxes_dir=settings.sandboxes_dir, db_path=settings.db_path
    )

    orchestrator: Orchestrator | None = None
    client: anthropic.AsyncAnthropic | None = None
    if settings.anthropic_api_key:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        registry.register(make_llm_ask_tool(client, ledger, settings.llm_ask_model))

        async def broadcast(event_type: str, payload: dict) -> None:
            await hub.broadcast(event_type, payload)

        orchestrator = Orchestrator(
            client=client,
            registry=registry,
            gateway=gateway,
            ledger=ledger,
            plans=plans,
            broadcast=broadcast,
            planner_model=settings.planner_model,
            max_turns=settings.plan_max_turns,
            agents=agents,
            sandboxes=sandboxes,
        )
        log.info(
            "orchestrator_ready",
            planner_model=settings.planner_model,
            llm_ask_model=settings.llm_ask_model,
            tools=[t.name for t in registry.all()],
        )
    else:
        log.warning(
            "anthropic_api_key_missing",
            detail="pilkd is up but chat will reject until ANTHROPIC_API_KEY is set",
        )

    app.state.hub = hub
    app.state.ledger = ledger
    app.state.plans = plans
    app.state.registry = registry
    app.state.gateway = gateway
    app.state.agents = agents
    app.state.sandboxes = sandboxes
    app.state.orchestrator = orchestrator
    app.state.anthropic = client
    app.state.orchestrator_tasks = set()

    log.info("pilkd_ready", home=str(home), host=settings.host, port=settings.port)
    try:
        yield
    finally:
        if client is not None:
            await client.close()
        log.info("pilkd_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="pilkd",
        version=__version__,
        lifespan=lifespan,
    )

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
    app.include_router(plans_router)
    app.include_router(cost_router)
    app.include_router(agents_router)
    app.include_router(sandboxes_router)
    app.include_router(ws_router)
    return app
