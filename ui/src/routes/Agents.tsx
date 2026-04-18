import { useCallback, useEffect, useState } from "react";
import {
  AUTONOMY_PROFILES,
  fetchAgents,
  fetchConnectedAccounts,
  fetchGrants,
  pilk,
  runAgent,
  setAgentPolicy,
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

export default function Agents() {
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [task, setTask] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);
  const [justCreated, setJustCreated] = useState<string | null>(null);
  const [grantsByAgent, setGrantsByAgent] = useState<Record<string, string[]>>({});
  const [accountsById, setAccountsById] = useState<Record<string, ConnectedAccount>>(
    {},
  );

  const refresh = useCallback(() => {
    fetchAgents()
      .then((r) => {
        setAgents(r.agents);
        if (r.agents.length > 0 && selected === null) setSelected(r.agents[0].name);
      })
      .catch(() => {});
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
    });
  }, [refresh]);

  const current = agents.find((a) => a.name === selected) ?? null;

  const submit = async () => {
    if (!current || !task.trim()) return;
    setSubmitting(true);
    setFlash(null);
    try {
      await runAgent(current.name, task.trim());
      setTask("");
      setFlash("Queued — watch the plan stream on the Chat tab.");
    } catch (e: any) {
      setFlash(`Error: ${e?.message ?? e}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="tasks">
      <div className="tasks-list">
        <div className="tasks-list-head">Registered agents</div>
        {agents.length === 0 && (
          <div className="tasks-empty">
            No agents registered yet. Ask PILK in Chat: <em>"Build me a sales agent."</em>
          </div>
        )}
        {agents.map((a) => (
          <button
            key={a.name}
            className={`tasks-row ${selected === a.name ? "tasks-row--active" : ""} ${justCreated === a.name ? "tasks-row--just-created" : ""}`}
            onClick={() => setSelected(a.name)}
            title={a.name}
          >
            <div className="tasks-row-goal">{humanizeAgentName(a.name)}</div>
            <div className="tasks-row-meta">
              <span className={`tasks-row-status tasks-row-status--${a.state}`}>
                {humanizeAgentState(a.state)}
              </span>
              <span className="tasks-row-cost">v{a.version}</span>
            </div>
          </button>
        ))}
      </div>
      <div className="tasks-detail">
        {current ? (
          <>
            <div className="tasks-detail-head">
              <div className="tasks-detail-goal" title={current.name}>
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
            <div className="agent-run">
              <div className="agent-tools-head">Assign a task</div>
              <textarea
                value={task}
                onChange={(e) => setTask(e.target.value)}
                placeholder={`Tell ${humanizeAgentName(current.name)} what to do…`}
                rows={3}
                disabled={submitting}
              />
              <div className="chat-actions">
                <button
                  className="btn btn--primary"
                  onClick={submit}
                  disabled={submitting || !task.trim()}
                >
                  {submitting ? "Queueing…" : "Run"}
                </button>
              </div>
              {flash && <div className="agent-flash">{flash}</div>}
            </div>
          </>
        ) : (
          <div className="tasks-empty">Select an agent.</div>
        )}
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
