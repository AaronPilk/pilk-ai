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
from core.api.routes.brain import router as brain_router
from core.api.routes.browser import router as browser_router
from core.api.routes.chat_uploads import router as chat_uploads_router
from core.api.routes.coding import router as coding_http_router
from core.api.routes.cost import router as cost_router
from core.api.routes.governor import router as governor_router
from core.api.routes.health import router as health_router
from core.api.routes.integration_secrets import router as integration_secrets_router
from core.api.routes.integrations import router as integrations_router
from core.api.routes.logs import router as logs_router
from core.api.routes.memory import router as memory_router
from core.api.routes.migration import router as migration_router
from core.api.routes.plans import router as plans_router
from core.api.routes.sandboxes import router as sandboxes_router
from core.api.routes.sentinel import router as sentinel_router
from core.api.routes.supabase import router as supabase_router
from core.api.routes.system_status import router as system_status_router
from core.api.routes.telegram import router as telegram_router
from core.api.routes.timers import router as timers_router
from core.api.routes.triggers import router as triggers_router
from core.api.routes.voice import router as voice_router
from core.api.routes.xauusd_settings import router as xauusd_settings_router
from core.api.ws import router as ws_router
from core.chat import AttachmentStore
from core.clients import ClientStore, set_client_store
from core.coding import (
    AgentSDKEngine,
    APIEngine,
    ClaudeCodeBridge,
    CodexBridge,
    CodingRouter,
)
from core.config import get_settings
from core.db import ensure_schema
from core.governor import DailyBudget, Governor, Tier, Tiers, TierSpec
from core.governor.providers import build_providers
from core.identity import AccountsStore, GrantsStore
from core.identity.bootstrap import seed_identity_memory
from core.integrations.apple import (
    check_messages_status,
    make_contacts_tools,
    make_messages_tools,
)
from core.integrations.client_secrets import load_client, setup_hint
from core.integrations.ghl import (
    make_ghl_calendar_tools,
    make_ghl_contact_tools,
    make_ghl_conversation_tools,
    make_ghl_pipeline_tools,
    make_ghl_workflow_tools,
)
from core.integrations.google import (
    ROLES,
    make_calendar_tools,
    make_drive_tools,
    make_gmail_tools,
    migrate_legacy_if_needed,
)
from core.integrations.google.sheets import make_sheets_tools
from core.integrations.google.slides import make_slides_tools
from core.integrations.legacy_migration import migrate_batch_k_google_files
from core.integrations.linkedin import make_linkedin_tools
from core.integrations.meta import make_meta_tools
from core.integrations.notion import make_notion_tools
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
from core.sentinel import HeartbeatStore, IncidentStore, Supervisor
from core.sentinel.notify import Notifier as SentinelNotifier
from core.sentinel.remediate import RemediationResult
from core.supabase import SupabaseClient
from core.timers import TimerDaemon
from core.timers.store import TimerStore
from core.tools import Gateway, ToolRegistry
from core.tools.builtin import (
    COMPUTER_CONTROL_TOOLS,
    CREATIVE_TOOLS,
    GOOGLE_ADS_TOOLS,
    META_ADS_TOOLS,
    PRINT_DESIGN_TOOLS,
    SALES_OPS_TOOLS,
    TELEGRAM_TOOLS,
    UGC_TOOLS,
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
    make_memory_delete_tool,
    make_memory_list_tool,
    make_memory_remember_tool,
    make_sentinel_tools,
    make_timer_set_tool,
    make_xauusd_take_over_tool,
    net_fetch_tool,
    shell_exec_tool,
    trade_execute_tool,
)
from core.tools.builtin.delivery import make_agent_email_deliver_tool
from core.tools.builtin.delivery.email import recipients_in_allowlist
from core.tools.builtin.design import (
    elementor_validate_tool,
    html_export_tool,
    wordpress_push_tool,
)
from core.trading.xauusd.settings_store import (
    XAUUSDSettingsStore,
    set_xauusd_settings_store,
)
from core.triggers import TriggerRegistry, TriggerScheduler
from core.voice import StubSTT, StubTTS, VoicePipeline, VoiceStateMachine
from core.voice.drivers import STTDriver, TTSDriver
from core.voice.elevenlabs_driver import ElevenLabsTTS
from core.voice.openai_driver import OpenAISTT, OpenAITTS

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "agents"
CLIENTS_DIR = REPO_ROOT / "clients"
TRIGGERS_DIR = REPO_ROOT / "triggers"

# Cap the number of unacked incidents that get prepended to a planner
# system prompt. More than a handful and the brief becomes noise; if
# there really are 50 unacked incidents the operator has a bigger
# problem than prompt framing.
SENTINEL_BRIEF_MAX_INCIDENTS = 5
# Only promote incidents that rise above low-grade chatter into the
# orchestrator's system prompt. LOW severity stays logged but doesn't
# pollute every chat turn.
SENTINEL_BRIEF_MIN_SEVERITY_RANK = 1  # MED+


def _compose_sentinel_brief(incidents: IncidentStore) -> str:
    """Render unacked incidents as a tight bullet list the orchestrator
    can prepend to its system prompt.

    Empty string when there's nothing worth surfacing — the orchestrator
    uses that as the "all clear" signal and leaves the prompt alone.
    Runs on every chat turn so we keep it cheap: a single indexed SQL
    query + a couple dozen characters per row.
    """
    try:
        rows = incidents.recent(
            limit=SENTINEL_BRIEF_MAX_INCIDENTS,
            only_unacked=True,
        )
    except Exception:
        # Sentinel-store trouble must never keep the orchestrator from
        # running. A silent skip here just means PILK doesn't get the
        # brief for this turn — the incident, if any, is still in SQLite.
        return ""
    promoted = [
        r for r in rows
        if r.severity.rank() >= SENTINEL_BRIEF_MIN_SEVERITY_RANK
    ]
    if not promoted:
        return ""
    lines = ["[Sentinel situation report — unacked incidents]"]
    for inc in promoted:
        agent = inc.agent_name or "unknown"
        cause = inc.triage.likely_cause if inc.triage else None
        suffix = f" — {cause}" if cause else ""
        lines.append(
            f"- [{inc.severity.value}] {agent}: "
            f"{inc.summary}{suffix} (id={inc.id})"
        )
    lines.append(
        "When answering the operator, mention any of the above that's "
        "relevant to what they're asking. You can acknowledge or restart "
        "via the sentinel tools if you decide that's appropriate."
    )
    return "\n".join(lines)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    home = settings.resolve_home()
    configure_logging(settings.log_level, settings.logs_dir)
    log = get_logger("pilkd.startup")

    home.mkdir(parents=True, exist_ok=True)
    ensure_schema(settings.db_path)

    # Plant PILK's self-identity facts (acronym, north star, operator,
    # role) before anything boots. Idempotent — reseeds the same four
    # rows on every cold start so post-migration or post-Redeploy the
    # agent re-anchors without relying on prior chat context.
    seed_identity_memory(settings.db_path)

    hub = Hub()
    ledger = Ledger(settings.db_path)
    plans = PlanStore(settings.db_path)
    memory = MemoryStore(settings.db_path)
    # User-managed API keys for external integrations — read by tools
    # via `core.secrets.resolve_secret()` with env-var fallback, and
    # written via PUT /integration-secrets from Settings → API Keys.
    integration_secrets = IntegrationSecretsStore(settings.db_path)
    set_integration_secrets_store(integration_secrets)
    # Runtime toggles for the XAUUSD execution agent (execution_mode
    # etc.). Separate from integration_secrets because these aren't
    # secrets — the UI renders them as switches.
    xauusd_settings = XAUUSDSettingsStore(settings.db_path)
    set_xauusd_settings_store(xauusd_settings)

    # Per-client brand / WordPress / recipient config, read from YAML
    # files under clients/. Source of truth is the filesystem — no DB
    # mirror. Bad files log + skip; the daemon keeps booting.
    clients = ClientStore(CLIENTS_DIR)
    loaded, errors = clients.reload()
    set_client_store(clients)
    log.info("client_store_ready", loaded=loaded, errors=errors)

    # Sentinel supervisor state — reads Hub events, emits incidents.
    sentinel_heartbeats = HeartbeatStore(settings.db_path)
    sentinel_incidents = IncidentStore(
        db_path=settings.db_path,
        jsonl_path=home / "sentinel" / "incidents.jsonl",
    )

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

    # Creative-content toolkit (nano_banana image gen + higgsfield video
    # gen). Both surface a clean "not configured" outcome when their key
    # is unset, so registration is unconditional.
    for t in CREATIVE_TOOLS:
        registry.register(t)
    log.info("creative_registered", tools=[t.name for t in CREATIVE_TOOLS])

    # Meta Ads operator toolkit — full campaign/adset/ad/creative/
    # insights coverage. Handlers surface a clean "not configured"
    # outcome when meta_access_token / meta_ad_account_id are missing,
    # so registration stays unconditional.
    for t in META_ADS_TOOLS:
        registry.register(t)
    log.info("meta_ads_registered", tools=[t.name for t in META_ADS_TOOLS])

    # Google Ads operator toolkit — symmetric to Meta Ads. Unconditional
    # registration; handlers surface a clean "not configured" outcome
    # when the developer token / OAuth triplet / customer ID isn't set.
    for t in GOOGLE_ADS_TOOLS:
        registry.register(t)
    log.info(
        "google_ads_registered",
        tools=[t.name for t in GOOGLE_ADS_TOOLS],
    )

    # Print-design toolkit — HTML + Playwright PDF exporter for
    # flyers / cards / banners / posters / trade-show backdrops. No
    # external keys; renderer is pure Python + chromium (already in
    # the dependency set for the browser tool).
    for t in PRINT_DESIGN_TOOLS:
        registry.register(t)
    log.info(
        "print_design_registered",
        tools=[t.name for t in PRINT_DESIGN_TOOLS],
    )

    # UGC scout toolkit — Apify-backed IG/TikTok discovery + Hunter.io
    # email enrichment + CSV export. Registration is unconditional;
    # each handler surfaces a clean "not configured" outcome when
    # apify_api_token / hunter_io_api_key are missing.
    for t in UGC_TOOLS:
        registry.register(t)
    log.info("ugc_registered", tools=[t.name for t in UGC_TOOLS])

    # Telegram notification toolkit — every agent (and PILK itself)
    # can push to the operator without waiting for a chat turn. COMMS
    # risk on every send so the approval queue still gates noise.
    # Unconditional registration; handlers surface "not configured"
    # until telegram_bot_token + telegram_chat_id land in Settings.
    for t in TELEGRAM_TOOLS:
        registry.register(t)
    log.info(
        "telegram_registered",
        tools=[t.name for t in TELEGRAM_TOOLS],
    )

    # Computer-control toolkit — IRREVERSIBLE fs/shell/osascript
    # outside the workspace sandbox. The gate shares a single
    # instance so rate limit + audit log + token store are
    # consistent across the four tools.
    from core.policy.computer_control import build_default_gate
    from core.tools.builtin.computer_control import set_gate as _set_cc_gate
    computer_control_gate = build_default_gate(settings.home)
    _set_cc_gate(computer_control_gate)
    for t in COMPUTER_CONTROL_TOOLS:
        registry.register(t)
    log.info(
        "computer_control_registered",
        tools=[t.name for t in COMPUTER_CONTROL_TOOLS],
        daily_limit=computer_control_gate.daily_limit,
        audit_path=str(computer_control_gate.audit_path),
    )

    # Web-design toolkit — html_export emits the static bundle; the
    # wordpress_push tool ships it to a client's WP site as an Elementor
    # draft. Both stay unconditional: html_export has no external deps
    # and wordpress_push surfaces a clean "not configured" error when
    # the per-site secret isn't set.
    registry.register(html_export_tool)
    registry.register(wordpress_push_tool)
    registry.register(elementor_validate_tool)
    log.info(
        "design_registered",
        tools=["html_export", "wordpress_push", "elementor_validate"],
    )

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

    def _oauth_setup_hint(name: str) -> str | None:
        return setup_hint(name, settings=settings)

    oauth_flow = OAuthFlowManager(
        providers=oauth_providers,
        accounts=accounts,
        client_loader=_load_oauth_client,
        setup_hint_loader=_oauth_setup_hint,
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
    # Slides — user-role only. One tool, slides_create. Needs the
    # slides.edit + drive.file scopes on the connected Google account.
    for t in make_slides_tools(accounts):
        registry.register(t)
    # Sheets — user-role only. Two tools (sheets_create,
    # sheets_append_rows). Needs the sheets.edit + drive.file scopes;
    # the operator widens scope groups per-link in Settings.
    for t in make_sheets_tools(accounts):
        registry.register(t)
    log.info("google_drive_calendar_slides_sheets_registered")

    # Slack — one user-role tool today (post). Registers unconditionally;
    # the tool surfaces a "connect it in Settings" message at call time
    # if no Slack workspace is linked.
    for t in make_slack_tools(accounts):
        registry.register(t)
    log.info("slack_registered", linked=accounts.default("slack", "user") is not None)

    # agent_email_deliver — the shared "agent hands a work product to a
    # human" delivery path. Uses the 'system' Google account for the
    # from-address and enforces [{agent_name}] {task_description} as
    # the subject format. See the permanent trust-rule seeding below.
    agent_email_deliver = make_agent_email_deliver_tool(accounts)
    registry.register(agent_email_deliver)

    # Seed permanent trust rules so deliveries to a small internal
    # allowlist bypass approval. Permanent = live until daemon restart;
    # re-seeded on every boot so a compromised SQLite can't forge trust.
    # The allowlist is two operator addresses — anything else flows
    # through normal approval.
    trust.add(
        agent_name=None,
        tool_name="agent_email_deliver",
        ttl_seconds=None,
        permanent=True,
        created_by="system",
        reason="operator-scoped auto-approve for internal deliveries",
        predicate=recipients_in_allowlist(
            {"aaron@skyway.media", "pilkingtonent@gmail.com"}
        ),
        predicate_label=(
            "all recipients in {aaron@skyway.media, pilkingtonent@gmail.com}"
        ),
    )
    log.info("agent_email_deliver_registered", auto_approve_allowlist=2)

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

    # Notion — API-key integration (not OAuth). Tools resolve the key
    # via resolve_secret on every call, so a runtime update in Settings
    # → API Keys takes effect without restarting the daemon.
    for t in make_notion_tools():
        registry.register(t)
    log.info(
        "notion_registered",
        tools=["notion_read", "notion_write"],
    )

    # Go High Level — agency PIT-backed CRM. Pipeline / opportunity
    # tools ship here; contacts, conversations, calendars, and
    # workflows land in follow-up PRs (#75c-e). Handlers resolve the
    # PIT on every call so a runtime rotation via Settings → API Keys
    # doesn't need a daemon restart.
    ghl_pipeline_tools = make_ghl_pipeline_tools()
    for t in ghl_pipeline_tools:
        registry.register(t)
    log.info(
        "ghl_pipeline_tools_registered",
        tools=[t.name for t in ghl_pipeline_tools],
    )

    # Contacts CRUD + meta (PR #75c). 8 contact tools + 3 meta tools,
    # all driven by the same agency PIT. Registered as a sibling
    # factory so a future schema change in one doesn't cascade into
    # the other.
    ghl_contact_tools = make_ghl_contact_tools()
    for t in ghl_contact_tools:
        registry.register(t)
    log.info(
        "ghl_contact_tools_registered",
        tools=[t.name for t in ghl_contact_tools],
    )

    # Conversations — SMS, email, thread search + read. Same agency
    # PIT, registered as its own sibling factory so adding more
    # channels (WhatsApp, IG, etc.) in the future is an additive
    # change.
    ghl_conversation_tools = make_ghl_conversation_tools()
    for t in ghl_conversation_tools:
        registry.register(t)
    log.info(
        "ghl_conversation_tools_registered",
        tools=[t.name for t in ghl_conversation_tools],
    )

    # Calendars + appointments + workflows + tasks + tag reads.
    # Two sibling factories (calendar + workflow) so a scheduling
    # issue can't take down the automation surface and vice versa.
    ghl_calendar_tools = make_ghl_calendar_tools()
    for t in ghl_calendar_tools:
        registry.register(t)
    log.info(
        "ghl_calendar_tools_registered",
        tools=[t.name for t in ghl_calendar_tools],
    )
    ghl_workflow_tools = make_ghl_workflow_tools()
    for t in ghl_workflow_tools:
        registry.register(t)
    log.info(
        "ghl_workflow_tools_registered",
        tools=[t.name for t in ghl_workflow_tools],
    )


    # Apple Messages + Contacts — local macOS integrations (not OAuth).
    # Tools always register; handlers surface a clean error when the
    # chat.db isn't readable or osascript refuses (Full Disk Access /
    # Automation missing, non-macOS host, etc.).
    for t in make_messages_tools():
        registry.register(t)
    for t in make_contacts_tools():
        registry.register(t)
    apple_status = check_messages_status()
    log.info(
        "apple_messages_registered",
        available=apple_status.available,
        reason=apple_status.reason,
        tools=["messages_search_mine", "messages_read_thread", "messages_send"],
    )
    log.info(
        "apple_contacts_registered",
        tools=["contacts_search"],
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
        # xauusd_take_over is browser-session-bound, so it registers
        # only when Browserbase is available. The rest of XAUUSD_TOOLS
        # is always registered a few lines below — the agent degrades
        # gracefully when Browserbase is unconfigured.
        registry.register(make_xauusd_take_over_tool(browser_sessions))
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
    # Memory write tool — scoped WRITE_LOCAL so the orchestrator can
    # save user-stated preferences / facts / patterns during the
    # "Talk to PILK" interview (and anywhere else it's natural to
    # remember something). Registration is unconditional so the tool
    # is advertised in the schema regardless of API-key state.
    registry.register(make_memory_remember_tool(memory))
    # memory_list + memory_delete complete the CRUD cycle on the
    # structured memory store so the orchestrator can recall what
    # it knows + forget things the operator corrects, without
    # bouncing to the dashboard.
    registry.register(make_memory_list_tool(memory))
    registry.register(make_memory_delete_tool(memory))

    # Long-term brain vault — Obsidian-compatible markdown folder the
    # operator can also open visually. Auto-create + seed if missing,
    # then advertise four brain_* tools (read, write, search, list).
    from core.brain import Vault
    from core.tools.builtin.brain import make_brain_tools
    from core.tools.builtin.brain_ingest import make_brain_ingest_tools

    brain = Vault(settings.brain_vault_path)
    try:
        brain.ensure_initialized()
    except OSError as e:
        log.warning("brain_vault_init_failed", path=str(brain.root), error=str(e))
    for t in make_brain_tools(brain):
        registry.register(t)
    # Ingesters share the same Vault + write to the same tree, so
    # they're wired alongside the brain tools rather than as a
    # separate bundle.
    ingest_tools = make_brain_ingest_tools(brain, accounts=accounts)
    for t in ingest_tools:
        registry.register(t)
    log.info(
        "brain_ready",
        vault=str(brain.root),
        tools=[
            "brain_note_read", "brain_note_write", "brain_search",
            "brain_note_list", "brain_note_search_and_replace",
            *[t.name for t in ingest_tools],
        ],
    )

    # Auto-seed the vault with Claude Code transcripts on boot. Fire-
    # and-forget — the task logs its own completion + won't kill the
    # daemon if it fails. Operator can disable via
    # PILK_BRAIN_AUTO_INGEST_ON_BOOT=false.
    if settings.brain_auto_ingest_on_boot:
        from core.brain import auto_ingest as _brain_auto_ingest
        _brain_auto_ingest.spawn(brain)

    # Gmail auto-ingest piggybacks on the user-role OAuth binding. We
    # defer evaluating the binding until the task actually runs —
    # linking the account after boot should work without a restart.
    if settings.brain_auto_ingest_gmail_on_boot:
        from core.brain import auto_ingest as _brain_auto_ingest_gmail
        from core.tools.builtin.brain_ingest import _load_user_gmail_creds

        def _gmail_creds_loader():
            return _load_user_gmail_creds(accounts)

        _brain_auto_ingest_gmail.spawn_gmail(
            brain,
            _gmail_creds_loader,
            query=settings.brain_auto_ingest_gmail_query,
        )

    if settings.anthropic_api_key:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        registry.register(make_llm_ask_tool(client, ledger, settings.llm_ask_model))

        providers = build_providers(
            anthropic_client=client,
            openai_api_key=settings.openai_api_key,
            claude_code_binary=settings.claude_code_binary,
            enable_claude_code_chat=settings.enable_claude_code_chat,
        )
        # Sentinel → orchestrator handoff. Before the first planner
        # turn of any run, the orchestrator asks us "anything on fire?"
        # and we answer with a compact brief pulled straight from the
        # unacked incidents. The orchestrator prepends it to the system
        # prompt so PILK plans *around* current trouble instead of
        # finding out after-the-fact. Empty brief = all clear = no
        # prompt mutation.
        async def sentinel_context_fn() -> str:
            return _compose_sentinel_brief(sentinel_incidents)

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
            sentinel_context_fn=sentinel_context_fn,
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
        "claude-code": ClaudeCodeBridge(
            settings.claude_code_binary,
            max_turns=settings.claude_code_max_turns,
            permission_mode=settings.claude_code_permission_mode,
            max_budget_usd=(
                settings.claude_code_max_budget_usd
                if settings.claude_code_max_budget_usd > 0
                else None
            ),
            model=settings.claude_code_model,
        ),
        "codex": CodexBridge(
            settings.codex_binary,
            model=settings.codex_model,
            sandbox_mode=settings.codex_sandbox_mode,
            yolo=settings.codex_yolo,
        ),
        "agent-sdk": AgentSDKEngine(
            client=client, model=settings.coding_api_model
        ),
        "api": APIEngine(client=client, model=settings.coding_api_model),
    }
    coding_router = CodingRouter(engines=coding_engines, governor=governor)
    registry.register(make_code_task_tool(coding_router))
    log.info(
        "coding_engines_ready",
        engines=list(coding_engines.keys()),
        api_available=client is not None,
    )

    # Sentinel supervisor — wired to the AgentRegistry for restart
    # remediations + subscribed to the Hub for event-driven scans.
    async def _sentinel_restart(agent_name: str) -> RemediationResult:
        try:
            await agents.mark_state(agent_name, "ready")
            return RemediationResult(
                kind="restarted",
                ok=True,
                message=f"marked {agent_name} as ready",
            )
        except Exception as e:
            return RemediationResult(
                kind="restarted",
                ok=False,
                message=f"restart failed: {e}",
            )

    sentinel = Supervisor(
        heartbeats=sentinel_heartbeats,
        incidents=sentinel_incidents,
        notifier=SentinelNotifier(),
        restart_fn=_sentinel_restart,
        logs_dir=settings.logs_dir,
        llm_call=None,  # triage starts on heuristic fallback; wire
                        # Haiku via the governor in a follow-up once
                        # we've observed a day of real findings and
                        # know the prompt is stable.
        broadcast=broadcast,  # emits `sentinel.incident` on every new
                              # incident so the UI top-bar + orchestrator
                              # hear about trouble without polling.
    )
    for t in make_sentinel_tools(
        heartbeats=sentinel_heartbeats,
        incidents=sentinel_incidents,
        supervisor=sentinel,
    ):
        registry.register(t)
    hub.subscribe(sentinel.on_event)
    await sentinel.start()
    log.info("sentinel_ready", rules=len(sentinel._rules))

    # Trigger registry + scheduler. Walks ``triggers/`` at boot, mirrors
    # enabled-state in SQLite, and runs one background task that ticks
    # every ~30s for cron triggers + subscribes to the hub for event
    # triggers. Only starts when the orchestrator is live — a scheduler
    # without an orchestrator to call would just log + drop fires.
    triggers = TriggerRegistry(manifests_dir=TRIGGERS_DIR, db_path=settings.db_path)
    installed_triggers = await triggers.discover_and_install()
    log.info(
        "triggers_discovered",
        names=installed_triggers,
        count=len(installed_triggers),
    )
    trigger_scheduler: TriggerScheduler | None = None
    if orchestrator is not None:
        trigger_scheduler = TriggerScheduler(
            registry=triggers,
            hub=hub,
            agent_run=orchestrator.agent_run,
            broadcast=broadcast,
        )
        await trigger_scheduler.start()
        log.info("trigger_scheduler_ready")
    else:
        log.info(
            "trigger_scheduler_inactive",
            reason="orchestrator offline — cannot fire triggers",
        )

    # One-shot timers — restart-resilient reminders (SQLite-backed).
    # The daemon resolves a fresh Telegram client on every fire via a
    # closure so runtime secret updates land without a daemon restart.
    timers = TimerStore(settings.db_path)
    registry.register(make_timer_set_tool(timers))

    def _timer_telegram_client():
        from core.integrations.telegram import TelegramClient, TelegramConfig
        from core.secrets import resolve_secret
        token = resolve_secret("telegram_bot_token", settings.telegram_bot_token)
        chat_id = resolve_secret("telegram_chat_id", settings.telegram_chat_id)
        if not token or not chat_id:
            return None
        return TelegramClient(
            TelegramConfig(bot_token=token, chat_id=chat_id)
        )

    timer_daemon = TimerDaemon(
        store=timers,
        broadcast=broadcast,
        telegram_client_fn=_timer_telegram_client,
    )
    await timer_daemon.start()
    log.info("timer_daemon_ready")

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
    app.state.brain = brain
    app.state.computer_control = computer_control_gate
    app.state.integration_secrets = integration_secrets
    app.state.xauusd_settings = xauusd_settings
    app.state.clients = clients
    app.state.sentinel = sentinel
    app.state.sentinel_heartbeats = sentinel_heartbeats
    app.state.sentinel_incidents = sentinel_incidents
    app.state.triggers = triggers
    app.state.trigger_scheduler = trigger_scheduler
    app.state.timers = timers
    app.state.timer_daemon = timer_daemon
    # Expose the broadcast callable to routes that emit their own events
    # (e.g. `sentinel.incident.acked`). Nothing else needs this; routes
    # that already had a direct handle (orchestrator, approvals) still
    # receive `broadcast` via constructor injection.
    app.state.broadcast = broadcast
    app.state.coding_router = coding_router
    app.state.accounts = accounts
    app.state.grants = grants
    app.state.oauth_providers = oauth_providers
    app.state.oauth_flow = oauth_flow

    # Chat attachments — disk-backed store under PILK_HOME/temp/chat-uploads.
    # Wired here so the orchestrator can resolve attachment IDs on the
    # first turn without a dependency on the HTTP route's closure.
    chat_attachments = AttachmentStore(home)
    chat_attachments.ensure_layout()
    app.state.chat_attachments = chat_attachments

    # Telegram chat bridge — long-polls getUpdates and feeds inbound
    # messages into the orchestrator. Only starts when both bot token
    # and chat_id are available AND the operator hasn't opted out via
    # PILK_TELEGRAM_CHAT_BRIDGE_ENABLED=false. Resolves both values
    # via ``resolve_secret`` so a runtime update through Settings →
    # API Keys is picked up on the next daemon restart without
    # needing an env-var change.
    #
    # When the bridge is active the approvals bridge piggy-backs on
    # its long-poll loop for callback_query updates, so there's only
    # ever one ``getUpdates`` caller in the process.
    telegram_bridge = None
    telegram_approvals_bridge = None
    if settings.telegram_chat_bridge_enabled and orchestrator is not None:
        from core.integrations.telegram import TelegramConfig
        from core.io import TelegramApprovals, TelegramBridge
        from core.secrets import resolve_secret

        _tg_token = resolve_secret("telegram_bot_token", settings.telegram_bot_token)
        _tg_chat_id = resolve_secret("telegram_chat_id", settings.telegram_chat_id)
        if _tg_token and _tg_chat_id:
            from core.integrations.telegram import TelegramClient

            _tg_cfg = TelegramConfig(bot_token=_tg_token, chat_id=_tg_chat_id)
            # The approvals bridge owns its own client so its
            # sendMessage / editMessageText / answerCallbackQuery
            # calls are decoupled from the bridge's long-poll
            # connection pool.
            telegram_approvals_bridge = TelegramApprovals(
                client=TelegramClient(_tg_cfg),
                hub=hub,
                approvals=approvals,
                chat_id=_tg_chat_id,
            )
            telegram_approvals_bridge.start()
            telegram_bridge = TelegramBridge(
                config=_tg_cfg,
                orchestrator=orchestrator,
                hub=hub,
                state_path=home / "state" / "telegram-bridge.json",
                callback_handler=telegram_approvals_bridge.handle_callback,
            )
            await telegram_bridge.start()
            log.info(
                "telegram_bridge_ready",
                chat_id=_tg_chat_id,
                approvals_forwarded=True,
            )
        else:
            log.info(
                "telegram_bridge_inactive",
                reason="bot_token or chat_id missing",
            )
    else:
        log.info(
            "telegram_bridge_disabled",
            enabled=settings.telegram_chat_bridge_enabled,
            orchestrator=orchestrator is not None,
        )
    app.state.telegram_bridge = telegram_bridge
    app.state.telegram_approvals = telegram_approvals_bridge

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
        # Sentinel shuts down first so its scan task won't race teardown.
        hub.unsubscribe(sentinel.on_event)
        await sentinel.stop()
        if trigger_scheduler is not None:
            await trigger_scheduler.stop()
        await timer_daemon.stop()
        if telegram_bridge is not None:
            await telegram_bridge.stop()
        if telegram_approvals_bridge is not None:
            telegram_approvals_bridge.stop()
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
    app.include_router(system_status_router)
    app.include_router(plans_router)
    app.include_router(cost_router)
    app.include_router(agents_router)
    app.include_router(sandboxes_router)
    app.include_router(approvals_router)
    app.include_router(voice_router)
    app.include_router(telegram_router)
    app.include_router(timers_router)
    app.include_router(triggers_router)
    app.include_router(browser_router)
    app.include_router(governor_router)
    app.include_router(integrations_router)
    app.include_router(accounts_router)
    app.include_router(apple_router)
    app.include_router(memory_router)
    app.include_router(brain_router)
    app.include_router(migration_router)
    app.include_router(integration_secrets_router)
    app.include_router(chat_uploads_router)
    app.include_router(xauusd_settings_router)
    app.include_router(logs_router)
    app.include_router(sentinel_router)
    app.include_router(coding_http_router)
    app.include_router(supabase_router)
    app.include_router(ws_router)
    return app
