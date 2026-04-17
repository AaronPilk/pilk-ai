import { useCallback, useEffect, useState } from "react";
import {
  fetchBrowserSessions,
  fetchSandboxes,
  pilk,
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

export default function Sandboxes() {
  const [rows, setRows] = useState<SandboxRow[]>([]);
  const [active, setActive] = useState<BrowserSession[]>([]);
  const [browserEnabled, setBrowserEnabled] = useState<boolean>(true);

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
    });
  }, [refreshSandboxes, refreshBrowser]);

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
                <BrowserTile key={s.id} session={s} />
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

function BrowserTile({ session }: { session: BrowserSession }) {
  const title = session.page_title || session.current_url || "New browser";
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
    </div>
  );
}

