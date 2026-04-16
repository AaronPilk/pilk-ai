import { useEffect, useState } from "react";
import {
  fetchCostEntries,
  fetchCostSummary,
  pilk,
  type CostEntry,
  type CostSummary,
} from "../state/api";

export default function Cost() {
  const [summary, setSummary] = useState<CostSummary | null>(null);
  const [entries, setEntries] = useState<CostEntry[]>([]);

  const refresh = () => {
    fetchCostSummary().then(setSummary).catch(() => {});
    fetchCostEntries(50).then((r) => setEntries(r.entries)).catch(() => {});
  };

  useEffect(() => {
    refresh();
    return pilk.onMessage((m) => {
      if (m.type === "cost.updated") refresh();
    });
  }, []);

  return (
    <div className="cost">
      <div className="cost-summary">
        <SummaryCard label="Today" value={summary?.day_usd ?? 0} />
        <SummaryCard label="7-day" value={summary?.week_usd ?? 0} />
        <SummaryCard label="30-day" value={summary?.month_usd ?? 0} />
        <SummaryCard label="All time" value={summary?.total_usd ?? 0} />
      </div>
      <div className="cost-entries">
        <div className="cost-entries-head">Recent calls</div>
        {entries.length === 0 ? (
          <div className="tasks-empty">No billable activity yet.</div>
        ) : (
          <table className="cost-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Model</th>
                <th>In</th>
                <th>Out</th>
                <th>USD</th>
                <th>Plan</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id}>
                  <td>{new Date(e.occurred_at).toLocaleTimeString()}</td>
                  <td>{e.model ?? "—"}</td>
                  <td>{e.input_tokens ?? 0}</td>
                  <td>{e.output_tokens ?? 0}</td>
                  <td>${e.usd.toFixed(6)}</td>
                  <td className="cost-table-plan">{e.plan_id ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function SummaryCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="cost-card">
      <div className="cost-card-label">{label}</div>
      <div className="cost-card-value">${value.toFixed(4)}</div>
    </div>
  );
}
