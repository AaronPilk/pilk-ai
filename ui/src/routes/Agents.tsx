import { useCallback, useEffect, useState } from "react";
import {
  AUTONOMY_PROFILES,
  fetchAgents,
  fetchConnectedAccounts,
  fetchGrants,
  fetchSentinelSummary,
  pilk,
  setAgentPolicy,
  setIntegrationSecret,
  startOAuthConnection,
  type AgentIntegration,
  type AgentRow,
  type AutonomyProfile,
  type ConnectedAccount,
} from "../state/api";
import { Link } from "react-router-dom";
import {
  humanizeAgentName,
  humanizeAgentState,
  humanizeProvider,
  humanizeToolName,
  providerForTool,
} from "../lib/humanize";

/** Emoji avatars for each registered agent. Emoji are a deliberate
 * choice over icon assets or SVGs: they render everywhere without a
 * build step, they're universally legible (the "grandma test"), and
 * they give each agent a distinct personality that a round initial
 * avatar can't. Keep the list in sync with each agent's manifest;
 * unmapped agents fall back to a generic robot. */
const AGENT_AVATAR: Record<string, string> = {
  ads_audit_agent: "🕵️",
  creative_content_agent: "🎬",
  elementor_converter_agent: "🧩",
  file_organization_agent: "🗂️",
  google_ads_agent: "🔎",
  meta_ads_agent: "📣",
  pitch_deck_agent: "📊",
  sales_ops_agent: "💼",
  sentinel: "🛡️",
  ugc_outreach_agent: "📧",
  ugc_scout_agent: "🎥",
  web_design_agent: "🖌️",
  xauusd_execution_agent: "🪙",
};

/** One-sentence plain-English blurb. Goal: a brand-new user can scan
 * the gallery and know exactly what each agent does without reading a
 * 300-word manifest. Falls back to the manifest description when a
 * blurb isn't supplied here. */
const AGENT_BLURB: Record<string, string> = {
  ads_audit_agent:
    "Audits your paid-ads accounts and returns a scored fix list.",
  creative_content_agent:
    "Makes images and short videos from a text brief.",
  elementor_converter_agent:
    "Turns a web design into a WordPress Elementor template.",
  file_organization_agent:
    "Cleans up and organizes files in your workspace.",
  google_ads_agent:
    "Runs Google Ads search campaigns end-to-end.",
  meta_ads_agent:
    "Runs Meta (Facebook/Instagram) ad campaigns end-to-end.",
  pitch_deck_agent:
    "Builds pitch decks and presentations for clients.",
  sales_ops_agent:
    "Finds leads, enriches contacts, and runs outbound campaigns.",
  sentinel:
    "Watches every agent and flags problems to PILK.",
  ugc_outreach_agent:
    "Emails UGC shortlists with personalised outreach, one approval at a time.",
  ugc_scout_agent:
    "Finds UGC creators, scores their content, and shortlists them with emails.",
  web_design_agent:
    "Designs web pages and exports them ready for WordPress.",
  xauusd_execution_agent:
    "Trades gold (XAU/USD) through the broker.",
};

function avatarFor(name: string): string {
  return AGENT_AVATAR[name] ?? "🤖";
}

function blurbFor(agent: AgentRow): string {
  return AGENT_BLURB[agent.name] ?? (agent.description ?? "").split("\n")[0];
}

export default function Agents() {
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  // Sentinel lives a tier above the other agents — it watches them
  // rather than being one of them. We pull its unacked-incident count
  // separately so the supervisor row can surface "all quiet" vs
  // "3 open alerts" at a glance without the operator having to open
  // the agent card first.
  const [sentinelUnacked, setSentinelUnacked] = useState<number | null>(null);
  const [justCreated, setJustCreated] = useState<string | null>(null);
  const [grantsByAgent, setGrantsByAgent] = useState<Record<string, string[]>>({});
  const [accountsById, setAccountsById] = useState<Record<string, ConnectedAccount>>(
    {},
  );
  // Populated when the /agents fetch fails. Critical for debugging —
  // without this, a 401 from an expired Supabase session or a 500 from
  // a misconfigured backend looked identical to "no agents registered,"
  // which sent operators down the wrong rabbit hole.
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(() => {
    fetchAgents()
      .then((r) => {
        setAgents(r.agents);
        setFetchError(null);
        setLoaded(true);
      })
      .catch((e: unknown) => {
        const msg = e instanceof Error ? e.message : String(e);
        setFetchError(msg);
        setLoaded(true);
      });
    fetchGrants()
      .then((r) => {
        const out: Record<string, string[]> = {};
        for (const [agentName, g] of Object.entries(r.grants)) {
          out[agentName] = [...g.accounts].sort();
        }
        setGrantsByAgent(out);
      })
      .catch(() => setGrantsByAgent({}));
    fetchConnectedAccounts()
      .then((r) => {
        const out: Record<string, ConnectedAccount> = {};
        for (const a of r.accounts) out[a.account_id] = a;
        setAccountsById(out);
      })
      .catch(() => setAccountsById({}));
    fetchSentinelSummary()
      .then((s) => setSentinelUnacked(s.unacked_count))
      .catch(() => setSentinelUnacked(null));
  }, [selected]);

  useEffect(() => {
    refresh();
    return pilk.onMessage((m) => {
      if (m.type === "plan.completed" || m.type === "plan.created") refresh();
      if (
        m.type === "agent.created" ||
        m.type === "agent.grant_added" ||
        m.type === "agent.grant_removed" ||
        m.type === "account.linked" ||
        m.type === "account.removed"
      ) {
        refresh();
        if (m.type === "agent.created") {
          setSelected(m.name);
          setJustCreated(m.name);
          setTimeout(
            () => setJustCreated((c) => (c === m.name ? null : c)),
            4000,
          );
        }
      }
      // Keep the supervisor row in sync with the top-bar badge: a new
      // incident bumps the pill, an acknowledge knocks it back down.
      if (m.type === "sentinel.incident") {
        setSentinelUnacked((n) => (n ?? 0) + 1);
      } else if (m.type === "sentinel.incident.acked") {
        setSentinelUnacked((n) => Math.max(0, (n ?? 0) - 1));
      }
    });
  }, [refresh]);

  const current = agents.find((a) => a.name === selected) ?? null;

  // Gallery-first pattern (iOS-style): tapping a card swaps the whole
  // pane to that agent's detail. A back button flips back to the grid.
  // Keeps discovery simple for new operators while giving one-click
  // access to the dense detail view for anyone who needs it.
  if (fetchError) {
    return (
      <div className="agents-page">
        <div className="tasks-error" role="alert">
          <div className="tasks-error-title">Couldn't load agents</div>
          <div className="tasks-error-body">{fetchError}</div>
          <div className="tasks-error-hint">
            Common causes: Supabase session expired (reload the page),
            or <code>PILK_SUPABASE_JWT_SECRET</code> missing on the
            server. Check <code>/system/status</code> — that route is
            public and will tell you if pilkd itself is up.
          </div>
          <button className="btn btn--ghost" onClick={refresh}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (current === null) {
    // Gallery view
    const sentinel = agents.find((a) => a.name === "sentinel") ?? null;
    const workers = agents.filter((a) => a.name !== "sentinel");
    return (
      <div className="agents-page">
        {sentinel && (
          <div className="agents-supervisor-row">
            <div className="agents-supervisor-head">
              <span className="agents-supervisor-label">Supervisor</span>
              <span className="agents-supervisor-sub">
                Watches every other agent and flags problems to PILK.
              </span>
            </div>
            <button
              className={`agent-card agents-supervisor-card ${
                (sentinelUnacked ?? 0) > 0
                  ? "agents-supervisor-card--alert"
                  : ""
              }`}
              onClick={() => setSelected(sentinel.name)}
            >
              <div className="agent-card-avatar" aria-hidden>
                {avatarFor(sentinel.name)}
              </div>
              <div className="agent-card-body">
                <div className="agent-card-name">
                  {humanizeAgentName(sentinel.name)}
                </div>
                <div className="agent-card-blurb">{blurbFor(sentinel)}</div>
                <div className="agent-card-meta">
                  {sentinelUnacked === null ? (
                    <span className="agent-card-status agent-card-status--ready">
                      <span className="agent-card-status-dot" />
                      {humanizeAgentState(sentinel.state)}
                    </span>
                  ) : sentinelUnacked === 0 ? (
                    <span className="agent-card-status agent-card-status--ready">
                      <span className="agent-card-status-dot" />
                      All quiet
                    </span>
                  ) : (
                    <span className="agents-supervisor-alerts">
                      {sentinelUnacked}{" "}
                      {sentinelUnacked === 1 ? "alert" : "alerts"} open
                    </span>
                  )}
                  {sentinel.autonomy_profile && (
                    <span className="agent-card-autonomy">
                      {capitalize(sentinel.autonomy_profile)}
                    </span>
                  )}
                </div>
              </div>
            </button>
          </div>
        )}
        <div className="agents-page-head">
          <h1>Your agents</h1>
          <p>
            Tap a card to open it, assign a task, or wire up its integrations.
          </p>
        </div>
        {!loaded && (
          <div className="agents-empty">Loading agents…</div>
        )}
        {loaded && workers.length === 0 && (
          <div className="agents-empty">
            No agents yet. Ask PILK in Chat: <em>"Build me a sales agent."</em>
          </div>
        )}
        {loaded && workers.length > 0 && (
          <div className="agents-gallery">
            {workers.map((a) => (
              <button
                key={a.name}
                className={`agent-card ${
                  justCreated === a.name ? "agent-card--just-created" : ""
                }`}
                onClick={() => setSelected(a.name)}
              >
                <div className="agent-card-avatar" aria-hidden>
                  {avatarFor(a.name)}
                </div>
                <div className="agent-card-body">
                  <div className="agent-card-name">
                    {humanizeAgentName(a.name)}
                    {justCreated === a.name && (
                      <span className="agent-new-badge">new</span>
                    )}
                  </div>
                  <div className="agent-card-blurb">{blurbFor(a)}</div>
                  <div className="agent-card-meta">
                    <span
                      className={`agent-card-status agent-card-status--${a.state}`}
                    >
                      <span className="agent-card-status-dot" />
                      {humanizeAgentState(a.state)}
                    </span>
                    {a.autonomy_profile && (
                      <span className="agent-card-autonomy">
                        {capitalize(a.autonomy_profile)}
                      </span>
                    )}
                  </div>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  // Detail view (single agent selected)
  return (
    <div className="agents-page">
      <button
        type="button"
        className="agents-back"
        onClick={() => setSelected(null)}
        aria-label="Back to all agents"
      >
        ← All agents
      </button>
      <div className="agent-detail">
        <div className="agent-detail-hero">
          <div className="agent-detail-avatar" aria-hidden>
            {avatarFor(current.name)}
          </div>
          <div className="agent-detail-hero-body">
            <div className="agent-detail-name">
              {humanizeAgentName(current.name)}
              {justCreated === current.name && (
                <span className="agent-new-badge">new</span>
              )}
            </div>
            <div className="tasks-detail-meta">
              <span>v{current.version}</span>
              <span>{humanizeAgentState(current.state)}</span>
              {current.sandbox && (
                <span>Sandbox · {capitalize(current.sandbox.type)}</span>
              )}
              {current.budget && (
                <span>
                  ${current.budget.per_run_usd}/run · $
                  {current.budget.daily_usd}/day
                </span>
              )}
              {current.last_run_at && (
                <span>
                  Last run · {new Date(current.last_run_at).toLocaleString()}
                </span>
              )}
            </div>
          </div>
        </div>
        {current.description && (
          <div className="agent-desc">{current.description}</div>
        )}
        <AutonomyControl
              agent={current}
              onChange={(profile) => {
                setAgents((prev) =>
                  prev.map((a) =>
                    a.name === current.name
                      ? { ...a, autonomy_profile: profile }
                      : a,
                  ),
                );
              }}
            />
            <IntegrationsPanel
              agent={current}
              onConfigured={(name) => {
                setAgents((prev) =>
                  prev.map((a) =>
                    a.name === current.name
                      ? {
                          ...a,
                          integrations: (a.integrations ?? []).map((i) =>
                            i.name === name ? { ...i, configured: true } : i,
                          ),
                        }
                      : a,
                  ),
                );
              }}
            />
            {current.tools && current.tools.length > 0 && (
              <div className="agent-tools">
                <div className="agent-tools-head">Tools</div>
                <div className="agent-tools-list">
                  {current.tools.map((t) => (
                    <span key={t} className="agent-tool" title={t}>
                      {humanizeToolName(t)}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {current.sandbox?.capabilities && current.sandbox.capabilities.length > 0 && (
              <div className="agent-tools">
                <div className="agent-tools-head">Sandbox capabilities</div>
                <div className="agent-tools-list">
                  {current.sandbox.capabilities.map((c) => (
                    <span key={c} className="agent-tool agent-tool--cap">
                      {humanizeToolName(c)}
                    </span>
                  ))}
                </div>
              </div>
            )}
            <div className="agent-tools">
              <div className="agent-tools-head">What this agent can reach</div>
              {(() => {
                const toolsForProvider = new Map<string, string[]>();
                for (const toolName of current.tools ?? []) {
                  const p = providerForTool(toolName);
                  if (!p) continue;
                  const list = toolsForProvider.get(p) ?? [];
                  list.push(toolName);
                  toolsForProvider.set(p, list);
                }
                const grantedIds = new Set(
                  grantsByAgent[current.name] ?? [],
                );
                const grantedProviders = new Set(
                  [...grantedIds]
                    .map((id) => accountsById[id]?.provider)
                    .filter((p): p is string => Boolean(p)),
                );
                // If an agent has no provider-backed tools, fall back to
                // a quiet empty line rather than an empty chip row.
                if (toolsForProvider.size === 0) {
                  return (
                    <div className="agent-access-note">
                      This agent doesn't use any connected-account tools.
                      It only touches local workspace + model calls.
                    </div>
                  );
                }
                const permissive =
                  grantsByAgent[current.name] === undefined;
                return (
                  <>
                    {permissive && (
                      <div className="agent-access-note">
                        Permissive — this agent predates the account-grant
                        layer. Adding any grant below will flip it to
                        opt-in mode.
                      </div>
                    )}
                    <div className="agent-reach-list">
                      {Array.from(toolsForProvider.entries()).map(
                        ([provider, toolNames]) => {
                          const granted =
                            permissive || grantedProviders.has(provider);
                          const accountForProvider = Object.values(
                            accountsById,
                          ).find((a) => a.provider === provider);
                          const accountLabel =
                            accountForProvider?.email ??
                            accountForProvider?.label ??
                            null;
                          return (
                            <div
                              key={provider}
                              className={`agent-reach-row agent-reach-row--${
                                granted ? "granted" : "blocked"
                              }`}
                            >
                              <div className="agent-reach-head">
                                <span className="agent-reach-provider">
                                  {humanizeProvider(provider)}
                                </span>
                                <span
                                  className={`agent-reach-status agent-reach-status--${
                                    granted ? "granted" : "blocked"
                                  }`}
                                >
                                  {granted ? "Granted" : "Needs access"}
                                </span>
                              </div>
                              {accountLabel && granted && (
                                <div className="agent-reach-account">
                                  {accountLabel}
                                </div>
                              )}
                              <div className="agent-reach-tools">
                                {toolNames.map((t) => (
                                  <span
                                    key={t}
                                    className="agent-reach-tool"
                                    title={t}
                                  >
                                    {humanizeToolName(t)}
                                  </span>
                                ))}
                              </div>
                              {!granted && (
                                <Link
                                  to="/settings"
                                  className="agent-reach-cta"
                                >
                                  Grant access in Settings →
                                </Link>
                              )}
                            </div>
                          );
                        },
                      )}
                    </div>
                  </>
                );
              })()}
            </div>
            <div className="agent-delegation-note">
              <div className="agent-tools-head">How to use this agent</div>
              <p>
                PILK assigns tasks. Ask in{" "}
                <Link to="/chat">Chat</Link> — something like{" "}
                <em>
                  "use {humanizeAgentName(current.name)} to …"
                </em>{" "}
                — and PILK routes the work here, or fans it out across
                multiple agents if the job needs it.
              </p>
            </div>
      </div>
    </div>
  );
}

function capitalize(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

const PROFILE_BLURB: Record<AutonomyProfile, string> = {
  observer:
    "Read-only. Every outbound or stateful action needs an approval.",
  assistant:
    "Default. Reads, local writes, shell, and browser are auto-allowed; outbound comms and posts still ask.",
  operator:
    "Trusted for outbound web actions (API writes) without prompting. Messaging and finance still approve.",
  autonomous:
    "Trusted for outbound comms too (email, posts). Finance and irreversible actions still approve every time.",
};

function IntegrationsPanel({
  agent,
  onConfigured,
}: {
  agent: AgentRow;
  onConfigured: (name: string) => void;
}) {
  const integrations = agent.integrations ?? [];
  if (integrations.length === 0) return null;

  const apiKeys = integrations.filter((i) => i.kind === "api_key");
  const oauths = integrations.filter((i) => i.kind === "oauth");

  return (
    <div className="agent-tools agent-integrations">
      <div className="agent-tools-head">Integrations this agent needs</div>
      {oauths.length > 0 && (
        <div className="agent-integrations-group">
          <div className="agent-integrations-sub">OAuth accounts</div>
          {oauths.map((i) => (
            <OAuthIntegrationRow
              key={`${i.name}:${i.role}`}
              integration={i}
              onConfigured={() => onConfigured(i.name)}
            />
          ))}
        </div>
      )}
      {apiKeys.length > 0 && (
        <div className="agent-integrations-group">
          <div className="agent-integrations-sub">API keys</div>
          {apiKeys.map((i) => (
            <ApiKeyIntegrationRow
              key={i.name}
              integration={i}
              onConfigured={() => onConfigured(i.name)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ApiKeyIntegrationRow({
  integration,
  onConfigured,
}: {
  integration: AgentIntegration;
  onConfigured: () => void;
}) {
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(!integration.configured);

  const save = async () => {
    if (!value.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await setIntegrationSecret(integration.name, value.trim());
      setValue("");
      setEditing(false);
      onConfigured();
    } catch (e: any) {
      setError(e?.message ?? String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className={`agent-integration-row agent-integration-row--${
        integration.configured ? "configured" : "missing"
      }`}
    >
      <div className="agent-integration-head">
        <span className="agent-integration-label">{integration.label}</span>
        <span
          className={`agent-integration-chip agent-integration-chip--${
            integration.configured ? "configured" : "missing"
          }`}
        >
          {integration.configured ? "Configured ✓" : "Needs setup"}
        </span>
      </div>
      <div className="agent-integration-key-name">Key: {integration.name}</div>
      {integration.configured && !editing && (
        <button
          className="btn btn--ghost"
          onClick={() => setEditing(true)}
        >
          Rotate key
        </button>
      )}
      {editing && (
        <div className="agent-integration-form">
          <input
            type="password"
            placeholder="Paste key…"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            disabled={saving}
            autoComplete="off"
          />
          <button
            className="btn btn--primary"
            onClick={save}
            disabled={saving || !value.trim()}
          >
            {saving ? "Saving…" : "Save"}
          </button>
          {integration.configured && (
            <button
              className="btn btn--ghost"
              onClick={() => {
                setEditing(false);
                setValue("");
                setError(null);
              }}
              disabled={saving}
            >
              Cancel
            </button>
          )}
        </div>
      )}
      {integration.docs_url && (
        <a
          href={integration.docs_url}
          target="_blank"
          rel="noreferrer"
          className="agent-integration-docs"
        >
          Where to get this key →
        </a>
      )}
      {error && <div className="agent-flash">Error: {error}</div>}
    </div>
  );
}

function OAuthIntegrationRow({
  integration,
  onConfigured,
}: {
  integration: AgentIntegration;
  onConfigured: () => void;
}) {
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connect = async () => {
    setConnecting(true);
    setError(null);
    try {
      const r = await startOAuthConnection({
        provider: integration.name,
        role: integration.role ?? "user",
        make_default: true,
      });
      // Hand off to the provider — onConfigured will flip via the
      // account.linked WS event the parent page already listens for.
      window.location.href = r.auth_url;
    } catch (e: any) {
      setError(e?.message ?? String(e));
      setConnecting(false);
    }
  };

  return (
    <div
      className={`agent-integration-row agent-integration-row--${
        integration.configured ? "configured" : "missing"
      }`}
    >
      <div className="agent-integration-head">
        <span className="agent-integration-label">{integration.label}</span>
        <span
          className={`agent-integration-chip agent-integration-chip--${
            integration.configured ? "configured" : "missing"
          }`}
        >
          {integration.configured ? "Connected ✓" : "Needs connection"}
        </span>
      </div>
      {integration.scopes.length > 0 && (
        <div className="agent-integration-scopes">
          Scopes: {integration.scopes.join(", ")}
        </div>
      )}
      {!integration.configured && (
        <button
          className="btn btn--primary"
          onClick={() => {
            void connect();
            onConfigured();
          }}
          disabled={connecting}
        >
          {connecting ? "Opening consent…" : `Connect ${humanizeProvider(integration.name)}`}
        </button>
      )}
      {error && <div className="agent-flash">Error: {error}</div>}
    </div>
  );
}

function AutonomyControl({
  agent,
  onChange,
}: {
  agent: AgentRow;
  onChange: (profile: AutonomyProfile) => void;
}) {
  const current: AutonomyProfile = agent.autonomy_profile ?? "assistant";
  const [saving, setSaving] = useState<AutonomyProfile | null>(null);
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="agent-tools agent-autonomy">
      <div className="agent-tools-head">Autonomy profile</div>
      <div className="agent-autonomy-hint">
        Approvals ask about outcomes, not every click. Raise the profile
        to let this agent work with fewer interruptions.
      </div>
      <div className="agent-autonomy-row">
        {AUTONOMY_PROFILES.map((p) => (
          <button
            key={p}
            className={`agent-autonomy-chip ${
              current === p ? "agent-autonomy-chip--active" : ""
            }`}
            disabled={saving !== null}
            onClick={async () => {
              if (current === p) return;
              setSaving(p);
              setError(null);
              try {
                await setAgentPolicy(agent.name, p);
                onChange(p);
              } catch (e: any) {
                setError(e?.message ?? String(e));
              } finally {
                setSaving(null);
              }
            }}
          >
            {saving === p ? `${capitalize(p)}…` : capitalize(p)}
          </button>
        ))}
      </div>
      <div className="agent-autonomy-blurb">{PROFILE_BLURB[current]}</div>
      {error && <div className="agent-flash">Error: {error}</div>}
    </div>
  );
}
