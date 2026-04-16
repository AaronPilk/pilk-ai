import { useEffect, useState } from "react";
import { fetchSandboxes, pilk, type SandboxRow } from "../state/api";

const STATE_COLOR: Record<string, string> = {
  ready: "#4fbf7a",
  creating: "#e0b84a",
  running: "#e0b84a",
  suspended: "#9ba2b0",
  destroyed: "#606775",
  errored: "#e55a5a",
};

export default function Sandboxes() {
  const [rows, setRows] = useState<SandboxRow[]>([]);

  const refresh = () =>
    fetchSandboxes().then((r) => setRows(r.sandboxes)).catch(() => {});

  useEffect(() => {
    refresh();
    return pilk.onMessage((m) => {
      if (m.type === "plan.created" || m.type === "plan.completed") refresh();
    });
  }, []);

  return (
    <div className="cost">
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
                <td className="cost-table-plan">{s.id}</td>
                <td>{s.type}</td>
                <td>{s.agent_name ?? "—"}</td>
                <td>
                  <span
                    className="dot"
                    style={{
                      background: STATE_COLOR[s.state] ?? "#606775",
                      marginRight: 6,
                    }}
                  />
                  {s.state}
                </td>
                <td className="cost-table-plan">{s.workspace ?? "—"}</td>
                <td>{new Date(s.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
