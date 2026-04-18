import { useCallback, useEffect, useState } from "react";
import {
  cancelPlan,
  fetchBrowserSessions,
  fetchSandboxes,
  pilk,
  type BrowserAction,
  type BrowserSession,
  type SandboxRow,
} from "../state/api";
import {
  humanize,
  humanizeAgentName,
  humanizeSandboxId,
  shortenPath,
} from "../lib/humanize";

const STATE_COLOR: Record<string, string> = {
  ready: "#65d19b",
  creating: "#f0c050",
  running: "#f0c050",
  suspended: "#a2a8b8",
  destroyed: "#6b7183",
  errored: "#ff6b6b",
};

interface ActionLogEntry extends BrowserAction {
  id: number;
}

export default function Sandboxes() {
  const [rows, setRows] = useState<SandboxRow[]>([]);
  const [active, setActive] = useState<BrowserSession[]>([]);
  const [browserEnabled, setBrowserEnabled] = useState<boolean>(true);
  // Keep a small per-session action log so the live tile can narrate what
  // the sandbox is doing. Capped at 10 most-recent entries per session to
  // keep DOM lean.
  const [actions, setActions] = useState<Record<string, ActionLogEntry[]>>({});
  const [stoppingPlan, setStoppingPlan] = useState<string | null>(null);

  const refreshSandboxes = useCallback(() => {
    fetchSandboxes()
      .then((r) => setRows(r.sandboxes))
      .catch(() => {});
  }, []);

  const refreshBrowser = useCallback(() => {
    fetchBrowserSessions()
      .then((r) => {
        setActive(r.active);
        setBrowserEnabled(r.enabled);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    refreshSandboxes();
    refreshBrowser();
    let seq = 0;
    return pilk.onMessage((m) => {
      if (m.type === "plan.created" || m.type === "plan.completed") {
        refreshSandboxes();
      }
      if (
        m.type === "browser.session_opened" ||
        m.type === "browser.session_closed" ||
        m.type === "browser.session_updated"
      ) {
        refreshBrowser();
      }
      if (m.type === "browser.action" && m.session_id) {
        const entry: ActionLogEntry = {
          id: ++seq,
          session_id: m.session_id,
          plan_id: m.plan_id ?? null,
          agent_name: m.agent_name ?? null,
          action: m.action,
          detail: m.detail ?? {},
          at: m.at ?? Date.now() / 1000,
        };
        setActions((prev) => {
          const next = { ...prev };
          const cur = next[entry.session_id] ?? [];
          next[entry.session_id] = [entry, ...cur].slice(0, 10);
          return next;
        });
      }
      if (m.type === "plan.completed" || m.type === "plan.cancelling") {
        setStoppingPlan((cur) => (cur === m.plan_id || cur === m.id ? null : cur));
      }
    });
  }, [refreshSandboxes, refreshBrowser]);

  const handleStopPlan = async (planId: string) => {
    setStoppingPlan(planId);
    try {
      await cancelPlan(planId);
    } catch {
      setStoppingPlan((cur) => (cur === planId ? null : cur));
    }
  };

  return (
    <div className="cost">
      {browserEnabled && (
        <section className="browser-live">
          <div className="cost-entries-head">Live browser</div>
          {active.length === 0 ? (
            <div className="browser-live-empty">
              No browser session running. Ask PILK to scrape a site, research
              something, or fill a form — it'll open a Chrome session here.
            </div>
          ) : (
            <div className="browser-tiles">
              {active.map((s) => (
                <BrowserTile
                  key={s.id}
                  session={s}
                  actions={actions[s.id] ?? []}
                  onStopPlan={handleStopPlan}
                  stoppingPlan={stoppingPlan}
                />
              ))}
            </div>
          )}
        </section>
      )}

      <section>
        <div className="cost-entries-head">Sandboxes</div>
        {rows.length === 0 ? (
          <div className="tasks-empty">
            No sandboxes yet. Run an agent from the Agents tab to create one.
          </div>
        ) : (
          <table className="cost-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Type</th>
                <th>Agent</th>
                <th>State</th>
                <th>Workspace</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.id}>
                  <td className="cost-table-plan" title={s.id}>
                    {humanizeSandboxId(s.id)}
                  </td>
                  <td>{humanize(s.type)}</td>
                  <td>{s.agent_name ? humanizeAgentName(s.agent_name) : "—"}</td>
                  <td>
                    <span
                      className="dot"
                      style={{
                        background: STATE_COLOR[s.state] ?? "#6b7183",
                        marginRight: 6,
                      }}
                    />
                    {humanize(s.state)}
                  </td>
                  <td className="cost-table-plan" title={s.workspace ?? ""}>
                    {s.workspace ? shortenPath(s.workspace) : "—"}
                  </td>
                  <td>{new Date(s.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function BrowserTile({
  session,
  actions,
  onStopPlan,
  stoppingPlan,
}: {
  session: BrowserSession;
  actions: ActionLogEntry[];
  onStopPlan: (planId: string) => void;
  stoppingPlan: string | null;
}) {
  const title = session.page_title || session.current_url || "New browser";
  const canStopPlan =
    session.plan_id !== null && session.plan_id !== undefined;
  const planBusy = stoppingPlan === session.plan_id;
  return (
    <div className="browser-tile">
      <div className="browser-tile-chrome">
        <div className="browser-tile-dots">
          <span className="browser-dot browser-dot--r" />
          <span className="browser-dot browser-dot--y" />
          <span className="browser-dot browser-dot--g" />
        </div>
        <div className="browser-tile-url" title={session.current_url ?? ""}>
          {title}
        </div>
        <div className="browser-tile-agent">
          {session.agent_name ? humanizeAgentName(session.agent_name) : "PILK"}
        </div>
        {canStopPlan && (
          <button
            className="browser-tile-stop"
            onClick={() => onStopPlan(session.plan_id!)}
            disabled={planBusy}
            title="Stop the plan driving this browser."
          >
            {planBusy ? "Stopping…" : "Stop"}
          </button>
        )}
      </div>
      {session.live_view_url ? (
        <iframe
          className="browser-tile-frame"
          src={session.live_view_url}
          sandbox="allow-same-origin allow-scripts allow-forms allow-popups"
          allow="clipboard-read; clipboard-write"
          title={`browser session ${session.id}`}
        />
      ) : (
        <div className="browser-tile-empty">
          Session {session.id.slice(0, 10)}… waiting for live view
        </div>
      )}
      <ActionStrip session={session} actions={actions} />
    </div>
  );
}

function ActionStrip({
  session,
  actions,
}: {
  session: BrowserSession;
  actions: ActionLogEntry[];
}) {
  const latest = actions[0];
  const label = latest
    ? formatAction(latest)
    : session.last_action
      ? session.last_action
      : "idle";
  return (
    <div className="browser-tile-actions">
      <div className="browser-tile-actions-head">
        <span className="browser-tile-actions-dot" />
        <span className="browser-tile-actions-current">{label}</span>
      </div>
      {actions.length > 1 && (
        <ul className="browser-tile-actions-list">
          {actions.slice(1, 6).map((a) => (
            <li key={a.id} className="browser-tile-actions-entry">
              <span className="browser-tile-actions-verb">{a.action}</span>
              <span className="browser-tile-actions-detail">
                {formatActionDetail(a)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function formatAction(a: ActionLogEntry): string {
  const detail = formatActionDetail(a);
  return detail ? `${a.action} — ${detail}` : a.action;
}

function formatActionDetail(a: ActionLogEntry): string {
  const d = a.detail as Record<string, unknown>;
  if (typeof d.title === "string" && d.title.length > 0) return d.title;
  if (typeof d.url === "string" && d.url.length > 0) {
    try {
      return new URL(d.url).host;
    } catch {
      return d.url;
    }
  }
  if (typeof d.text === "string") return d.text;
  return "";
}
