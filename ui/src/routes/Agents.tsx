import { useCallback, useEffect, useState } from "react";
import {
  fetchAgents,
  fetchConnectedAccounts,
  fetchGrants,
  pilk,
  runAgent,
  type AgentRow,
  type ConnectedAccount,
} from "../state/api";
import {
  humanizeAgentName,
  humanizeAgentState,
  humanizeToolName,
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
              <div className="agent-tools-head">Account access</div>
              {grantsByAgent[current.name] === undefined ? (
                <div className="agent-access-note">
                  No explicit grants set. Inherited from before access control
                  was introduced — manage grants in Settings → Connected
                  accounts to lock this down.
                </div>
              ) : grantsByAgent[current.name].length === 0 ? (
                <div className="agent-access-note">
                  No accounts granted. This agent cannot use any connected
                  account until you grant access in Settings → Connected
                  accounts.
                </div>
              ) : (
                <div className="agent-tools-list">
                  {grantsByAgent[current.name].map((id) => {
                    const a = accountsById[id];
                    return (
                      <span key={id} className="agent-tool agent-tool--account">
                        {a?.email ?? a?.label ?? id}
                      </span>
                    );
                  })}
                </div>
              )}
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
