import { useCallback, useEffect, useState } from "react";
import { fetchAgents, pilk, runAgent, type AgentRow } from "../state/api";
import { humanizeAgentName, humanizeToolName } from "../lib/humanize";

export default function Agents() {
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [task, setTask] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);
  const [justCreated, setJustCreated] = useState<string | null>(null);

  const refresh = useCallback(() => {
    fetchAgents()
      .then((r) => {
        setAgents(r.agents);
        if (r.agents.length > 0 && selected === null) setSelected(r.agents[0].name);
      })
      .catch(() => {});
  }, [selected]);

  useEffect(() => {
    refresh();
    return pilk.onMessage((m) => {
      if (m.type === "plan.completed" || m.type === "plan.created") refresh();
      if (m.type === "agent.created") {
        refresh();
        setSelected(m.name);
        setJustCreated(m.name);
        setTimeout(() => setJustCreated((current) => (current === m.name ? null : current)), 4000);
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
                {capitalize(a.state)}
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
                <span>{capitalize(current.state)}</span>
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
