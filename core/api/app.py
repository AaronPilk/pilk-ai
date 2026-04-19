"""FastAPI app factory for pilkd.

Lifespan wires up the shared singletons: SQLite schema, the connection
hub, the cost ledger, the tool registry, the gateway, the plan store,
the agent registry, the sandbox manager, the policy/approval layer,
and (if an API key is present) the Anthropic client + orchestrator.

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
from core.api.auth import SupabaseJWTMiddleware
from core.api.hub import Hub
from core.api.routes.accounts import router as accounts_router
from core.api.routes.agents import router as agents_router
from core.api.routes.apple import router as apple_router
from core.api.routes.approvals import router as approvals_router
from core.api.routes.browser import router as browser_router
from core.api.routes.coding import router as coding_http_router
from core.api.routes.cost import router as cost_router
from core.api.routes.governor import router as governor_router
from core.api.routes.health import router as health_router
from core.api.routes.integration_secrets import router as integration_secrets_router
from core.api.routes.integrations import router as integrations_router
from core.api.routes.logs import router as logs_router
from core.api.routes.memory import router as memory_router
from core.api.routes.plans import router as plans_router
from core.api.routes.sandboxes import router as sandboxes_router
from core.api.routes.supabase import router as supabase_router
from core.api.routes.voice import router as voice_router
from core.api.ws import router as ws_router
from core.coding import (
    AgentSDKEngine,
    APIEngine,
    ClaudeCodeBridge,
    CodingRouter,
)
from core.config import get_settings
from core.db import ensure_schema
from core.governor import DailyBudget, Governor, Tier, Tiers, TierSpec
from core.governor.providers import build_providers
from core.identity import AccountsStore, GrantsStore
from core.integrations.apple import check_messages_status, make_messages_tools
from core.integrations.client_secrets import load_client
from core.integrations.google import (
    ROLES,
    make_calendar_tools,
    make_drive_tools,
    make_gmail_tools,
    migrate_legacy_if_needed,
)
from core.integrations.legacy_migration import migrate_batch_k_google_files
from core.integrations.linkedin import make_linkedin_tools
from core.integrations.meta import make_meta_tools
from core.integrations.oauth_flow import OAuthFlowManager
from core.integrations.provider import ProviderRegistry
from core.integrations.providers.google import google_provider
from core.integrations.providers.linkedin import linkedin_provider
from core.integrations.providers.meta import meta_provider
from core.integrations.providers.slack import slack_provider
from core.integrations.providers.x import x_provider
from core.integrations.slack import make_slack_tools
from core.integrations.x import make_x_tools
from core.ledger import Ledger
from core.logging import configure_logging, get_logger
from core.memory import MemoryStore
from core.orchestrator import Orchestrator, PlanStore
from core.policy import AgentPolicyStore, ApprovalManager, Gate, TrustStore
from core.registry import AgentRegistry
from core.sandbox import SandboxManager
from core.secrets import (
    IntegrationSecretsStore,
    set_integration_secrets_store,
)
from core.supabase import SupabaseClient
from core.tools import Gateway, ToolRegistry
from core.tools.builtin import (
    SALES_OPS_TOOLS,
    XAUUSD_TOOLS,
    BrowserSessionManager,
    finance_deposit_tool,
    finance_transfer_tool,
    finance_withdraw_tool,
    fs_read_tool,
    fs_write_tool,
    make_agent_create_tool,
    make_browser_tools,
    make_code_task_tool,
    make_llm_ask_tool,
    net_fetch_tool,
    shell_exec_tool,
    trade_execute_tool,
)
from core.voice import StubSTT, StubTTS, VoicePipeline, VoiceStateMachine
from core.voice.drivers import STTDriver, TTSDriver
from core.voice.elevenlabs_driver import ElevenLabsTTS
from core.voice.openai_driver import OpenAISTT, OpenAITTS

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
    memory = MemoryStore(settings.db_path)
    # User-managed API keys for external integrations — read by tools
    # via `core.secrets.resolve_secret()` with env-var fallback, and
    # written via PUT /integration-secrets from Settings → API Keys.
    integration_secrets = IntegrationSecretsStore(settings.db_path)
    set_integration_secrets_store(integration_secrets)

    async def broadcast(event_type: str, payload: dict) -> None:
        await hub.broadcast(event_type, payload)

    trust = TrustStore()
    agent_policies = AgentPolicyStore(settings.db_path)
    await agent_policies.hydrate()
    approvals = ApprovalManager(
        db_path=settings.db_path, trust_store=trust, broadcast=broadcast
    )

    # Governor: tier config + daily budget + session override.
    tiers = Tiers(
        light=TierSpec(
            tier=Tier.LIGHT,
            provider=settings.tier_light_provider,
            model=settings.tier_light_model,
        ),
        standard=TierSpec(
            tier=Tier.STANDARD,
            provider=settings.tier_standard_provider,
            model=settings.tier_standard_model,
        ),
        premium=TierSpec(
            tier=Tier.PREMIUM,
            provider=settings.tier_premium_provider,
            model=settings.tier_premium_model,
        ),
    )
    governor = Governor(
        tiers=tiers,
        budget=DailyBudget(settings.db_path, cap_usd=settings.daily_cap_usd),
        premium_gate="ask" if settings.premium_gate != "auto" else "auto",
        db_path=settings.db_path,
    )
    await governor.hydrate_from_db()
    log.info(
        "governor_ready",
        light=f"{tiers.light.provider}/{tiers.light.model}",
        standard=f"{tiers.standard.provider}/{tiers.standard.model}",
        premium=f"{tiers.premium.provider}/{tiers.premium.model}",
        daily_cap_usd=settings.daily_cap_usd,
        premium_gate=settings.premium_gate,
    )

    registry = ToolRegistry()
    registry.register(fs_read_tool)
    registry.register(fs_write_tool)
    registry.register(shell_exec_tool)
    registry.register(net_fetch_tool)
    registry.register(finance_deposit_tool)
    registry.register(finance_withdraw_tool)
    registry.register(finance_transfer_tool)
    registry.register(trade_execute_tool)
    # Sales-ops toolkit (prospecting, site audit, email enrichment, HubSpot).
    # Handlers surface a clean "not configured" error if the matching API
    # key is missing, so registration stays unconditional.
    for t in SALES_OPS_TOOLS:
        registry.register(t)
    log.info("sales_ops_registered", tools=[t.name for t in SALES_OPS_TOOLS])
    # XAU/USD execution-agent toolkit. Analysis + risk + state tools are
    # live; the broker tools (place_order, flatten_all) refuse in paper
    # mode and stay that way until core/trading/xauusd/config.py is
    # edited AND a Hugosway Browserbase adapter ships.
    for t in XAUUSD_TOOLS:
        registry.register(t)
    log.info("xauusd_registered", tools=[t.name for t in XAUUSD_TOOLS])

    # Connected accounts: one AccountsStore + one ProviderRegistry for
    # every OAuth-backed integration. Provider-specific tool factories
    # (Gmail, future Slack, etc.) bind to the store via AccountBinding
    # and resolve the live account at call time.
    migrate_legacy_if_needed(home)                # Batch K → role files
    accounts = AccountsStore(home)
    accounts.ensure_layout()
    grants = GrantsStore(home)
    oauth_providers = ProviderRegistry()
    oauth_providers.register(google_provider)
    oauth_providers.register(slack_provider)
    oauth_providers.register(linkedin_provider)
    oauth_providers.register(x_provider)
    oauth_providers.register(meta_provider)
    migrated = migrate_batch_k_google_files(home, accounts)
    if migrated:
        log.info("accounts_legacy_imported", account_ids=migrated)

    def _load_oauth_client(name: str) -> tuple[str, str] | None:
        return load_client(name, settings=settings)

    oauth_flow = OAuthFlowManager(
        providers=oauth_providers,
        accounts=accounts,
        client_loader=_load_oauth_client,
        public_base_url=f"http://{settings.host}:{settings.port}",
    )

    # Gmail tools bind to (google, role) via the store. Registration is
    # unconditional — if no account is linked for a role, the tool
    # itself surfaces a friendly "connect it in Settings" message at
    # call time. This keeps the tool list stable across link/unlink.
    for role in ROLES:
        for t in make_gmail_tools(role, accounts):
            registry.register(t)
        linked = accounts.default("google", role)
        log.info(
            "gmail_role_registered",
            role=role,
            linked_email=linked.email if linked else None,
        )
    # Drive + Calendar are user-role only. Registered unconditionally so
    # the tool list stays stable; each tool surfaces an "Expand access"
    # hint if the matching scope group isn't enabled on the account.
    for t in make_drive_tools(accounts):
        registry.register(t)
    for t in make_calendar_tools(accounts):
        registry.register(t)
    log.info("google_drive_calendar_registered")

    # Slack — one user-role tool today (post). Registers unconditionally;
    # the tool surfaces a "connect it in Settings" message at call time
    # if no Slack workspace is linked.
    for t in make_slack_tools(accounts):
        registry.register(t)
    log.info("slack_registered", linked=accounts.default("slack", "user") is not None)

    # LinkedIn + X — one post tool each, user-role only. Same pattern:
    # always register, tool handler surfaces "connect in Settings" if no
    # account is linked. Adding a new social provider is one provider
    # file + one tool file; the framework doesn't need to change.
    for t in make_linkedin_tools(accounts):
        registry.register(t)
    log.info(
        "linkedin_registered",
        linked=accounts.default("linkedin", "user") is not None,
    )
    for t in make_x_tools(accounts):
        registry.register(t)
    log.info("x_registered", linked=accounts.default("x", "user") is not None)
    for t in make_meta_tools(accounts):
        registry.register(t)
    log.info(
        "meta_registered",
        linked=accounts.default("meta", "user") is not None,
    )

    # Apple Messages — local macOS integration (not OAuth). Tools
    # always register; handlers surface a clean error when the chat.db
    # isn't readable (Full Disk Access missing, non-macOS host, etc.).
    for t in make_messages_tools():
        registry.register(t)
    apple_status = check_messages_status()
    log.info(
        "apple_messages_registered",
        available=apple_status.available,
        reason=apple_status.reason,
    )

    browser_sessions: BrowserSessionManager | None = None
    if settings.browserbase_api_key and settings.browserbase_project_id:
        browser_sessions = BrowserSessionManager(
            api_key=settings.browserbase_api_key,
            project_id=settings.browserbase_project_id,
            broadcast=broadcast,
        )
        for t in make_browser_tools(browser_sessions):
            registry.register(t)
        log.info("browserbase_ready", project=settings.browserbase_project_id)
    else:
        log.info(
            "browserbase_disabled",
            detail="set BROWSERBASE_API_KEY + BROWSERBASE_PROJECT_ID to enable",
        )

    async def on_step_status(step_id: str, status: str) -> None:
        updated = await plans.set_step_status(step_id, status=status)
        await broadcast("plan.step_updated", updated)

    gateway = Gateway(
        registry,
        Gate(trust=trust, agent_profile_lookup=agent_policies.get),
        approvals=approvals,
        on_step_status=on_step_status,
        accounts=accounts,
        grants=grants,
    )

    agents = AgentRegistry(manifests_dir=AGENTS_DIR, db_path=settings.db_path)
    installed = await agents.discover_and_install()
    log.info("agents_discovered", names=installed, count=len(installed))

    sandboxes = SandboxManager(
        sandboxes_dir=settings.sandboxes_dir, db_path=settings.db_path
    )

    # Register the COO meta-tool. This lives only on the top-level chat
    # path — agents cannot spawn other agents (enforced in Manifest).
    registry.register(
        make_agent_create_tool(
            tool_registry=registry,
            agent_registry=agents,
            sandboxes=sandboxes,
            agents_dir=AGENTS_DIR,
            broadcast=broadcast,
            grants=grants,
        )
    )

    orchestrator: Orchestrator | None = None
    client: anthropic.AsyncAnthropic | None = None
    if settings.anthropic_api_key:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        registry.register(make_llm_ask_tool(client, ledger, settings.llm_ask_model))

        providers = build_providers(
            anthropic_client=client,
            openai_api_key=settings.openai_api_key,
        )
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
            governor=governor,
            providers=providers,
        )
        log.info(
            "orchestrator_ready",
            planner_model=settings.planner_model,
            llm_ask_model=settings.llm_ask_model,
            providers=sorted(providers.keys()),
            tools=[t.name for t in registry.all()],
        )
    else:
        log.warning(
            "anthropic_api_key_missing",
            detail="pilkd is up but chat will reject until ANTHROPIC_API_KEY is set",
        )

    # Coding engines: PILK orchestrates; engines are pluggable backends
    # for repo-scope vs snippet-scope code work. Claude Code + Agent SDK
    # are scaffolded and report unavailable until wired in follow-up
    # batches; the API engine is the only one that actually runs today.
    coding_engines: dict = {
        "claude-code": ClaudeCodeBridge(settings.claude_code_bridge_url),
        "agent-sdk": AgentSDKEngine(),
        "api": APIEngine(client=client, model=settings.coding_api_model),
    }
    coding_router = CodingRouter(engines=coding_engines, governor=governor)
    registry.register(make_code_task_tool(coding_router))
    log.info(
        "coding_engines_ready",
        engines=list(coding_engines.keys()),
        api_available=client is not None,
    )

    voice_state = VoiceStateMachine(broadcast=broadcast)
    stt: STTDriver = (
        OpenAISTT(settings.openai_api_key) if settings.openai_api_key else StubSTT()
    )
    tts: TTSDriver
    if settings.elevenlabs_api_key:
        if settings.elevenlabs_voice_id:
            tts = ElevenLabsTTS(
                settings.elevenlabs_api_key,
                voice_id=settings.elevenlabs_voice_id,
            )
        else:
            tts = ElevenLabsTTS(settings.elevenlabs_api_key)
    elif settings.openai_api_key:
        tts = OpenAITTS(
            settings.openai_api_key,
            voice=settings.tts_voice or "alloy",
        )
    else:
        tts = StubTTS()
    voice_pipeline = VoicePipeline(
        state=voice_state,
        stt=stt,
        tts=tts,
        orchestrator=orchestrator,
        ledger=ledger,
    )
    log.info(
        "voice_ready",
        stt=stt.name,
        tts=tts.name,
        orchestrator=orchestrator is not None,
    )

    app.state.hub = hub
    app.state.ledger = ledger
    app.state.plans = plans
    app.state.registry = registry
    app.state.gateway = gateway
    app.state.agents = agents
    app.state.sandboxes = sandboxes
    app.state.trust = trust
    app.state.approvals = approvals
    app.state.agent_policies = agent_policies
    app.state.orchestrator = orchestrator
    app.state.anthropic = client
    app.state.voice_state = voice_state
    app.state.voice_pipeline = voice_pipeline
    app.state.orchestrator_tasks = set()
    app.state.browser_sessions = browser_sessions
    app.state.governor = governor
    app.state.memory = memory
    app.state.integration_secrets = integration_secrets
    app.state.coding_router = coding_router
    app.state.accounts = accounts
    app.state.grants = grants
    app.state.oauth_providers = oauth_providers
    app.state.oauth_flow = oauth_flow

    # Supabase foundation — stays None-like when unconfigured. Nothing
    # in the runtime path depends on it yet; only GET /supabase/health
    # reads it.
    supabase = SupabaseClient.from_settings(settings)
    app.state.supabase = supabase
    log.info(
        "supabase_foundation",
        configured=supabase.is_configured,
        has_service_role=bool(supabase.service_role_key),
    )

    log.info("pilkd_ready", home=str(home), host=settings.host, port=settings.port)
    try:
        yield
    finally:
        if browser_sessions is not None:
            await browser_sessions.close_all()
        if client is not None:
            await client.close()
        log.info("pilkd_shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="pilkd",
        version=__version__,
        lifespan=lifespan,
    )

    # Middleware execution order is reverse of registration: auth must
    # wrap *inside* CORS so preflight OPTIONS requests (which have no
    # Authorization header) get answered by the CORS layer instead of
    # being rejected as unauthenticated.
    if settings.cloud:
        app.add_middleware(SupabaseJWTMiddleware, settings=settings)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(plans_router)
    app.include_router(cost_router)
    app.include_router(agents_router)
    app.include_router(sandboxes_router)
    app.include_router(approvals_router)
    app.include_router(voice_router)
    app.include_router(browser_router)
    app.include_router(governor_router)
    app.include_router(integrations_router)
    app.include_router(accounts_router)
    app.include_router(apple_router)
    app.include_router(memory_router)
    app.include_router(integration_secrets_router)
    app.include_router(logs_router)
    app.include_router(coding_http_router)
    app.include_router(supabase_router)
    app.include_router(ws_router)
    return app
