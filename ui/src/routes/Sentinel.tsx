import { useCallback, useEffect, useMemo, useState } from "react";
import {
  acknowledgeSentinelIncident,
  fetchSentinelIncidents,
  pilk,
  type SentinelIncident,
  type SentinelSeverity,
} from "../state/api";

type SeverityFilter = "all" | SentinelSeverity;

const FILTER_CHIPS: { key: SeverityFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "critical", label: "Critical" },
  { key: "high", label: "High" },
  { key: "med", label: "Medium" },
  { key: "low", label: "Low" },
];

export default function Sentinel() {
  const [entries, setEntries] = useState<SentinelIncident[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<SeverityFilter>("all");
  const [onlyUnacked, setOnlyUnacked] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await fetchSentinelIncidents({
        only_unacked: onlyUnacked,
        min_severity: filter === "all" ? undefined : filter,
        limit: 100,
      });
      setEntries(r.incidents);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [filter, onlyUnacked]);

  useEffect(() => {
    void load();
  }, [load]);

  // Live updates: refetch on any sentinel event so the list stays in
  // sync with the top-bar badge and with other tabs.
  useEffect(() => {
    return pilk.onMessage((m) => {
      if (
        m.type === "sentinel.incident" ||
        m.type === "sentinel.incident.acked"
      ) {
        void load();
      }
    });
  }, [load]);

  const handleAck = async (id: string) => {
    try {
      await acknowledgeSentinelIncident(id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const empty = useMemo(
    () =>
      !loading &&
      entries.length === 0 && (
        <div className="sentinel-empty">
          {onlyUnacked
            ? "All quiet — no unacknowledged incidents."
            : "No incidents recorded in this severity range."}
        </div>
      ),
    [loading, entries.length, onlyUnacked],
  );

  return (
    <div className="sentinel">
      <div className="sentinel-header">
        <h1>Sentinel</h1>
        <p className="sentinel-sub">
          Everything PILK's watchdog has caught. Acknowledge an incident once
          you've read it or dealt with it — the orchestrator stops mentioning
          acknowledged items in chat.
        </p>
      </div>

      <div className="sentinel-controls">
        {FILTER_CHIPS.map((c) => (
          <button
            key={c.key}
            className={`chip ${filter === c.key ? "chip--active" : ""}`}
            onClick={() => setFilter(c.key)}
          >
            {c.label}
          </button>
        ))}
        <label className="sentinel-toggle">
          <input
            type="checkbox"
            checked={onlyUnacked}
            onChange={(e) => setOnlyUnacked(e.target.checked)}
          />
          Only unacknowledged
        </label>
      </div>

      {err && <div className="sentinel-error">{err}</div>}
      {loading && <div className="sentinel-loading">Loading…</div>}
      {empty}

      <ul className="sentinel-list">
        {entries.map((inc) => (
          <li key={inc.id} className={`sentinel-row sev-${inc.severity}`}>
            <div className="sentinel-row-top">
              <span className={`sentinel-sev sentinel-sev--${inc.severity}`}>
                {inc.severity.toUpperCase()}
              </span>
              <span className="sentinel-agent">{inc.agent ?? "system"}</span>
              <span className="sentinel-category">{inc.category}</span>
              <span className="sentinel-time">
                {new Date(inc.created_at).toLocaleString()}
              </span>
            </div>
            <div className="sentinel-summary">{inc.summary}</div>
            {inc.likely_cause && (
              <div className="sentinel-cause">
                <strong>Likely cause:</strong> {inc.likely_cause}
              </div>
            )}
            {inc.recommended_action && (
              <div className="sentinel-action">
                <strong>Recommended:</strong> {inc.recommended_action}
              </div>
            )}
            {inc.remediation && (
              <div className="sentinel-remediation">
                <strong>Auto-remediation:</strong> {inc.remediation}
                {inc.outcome ? ` — ${inc.outcome}` : ""}
              </div>
            )}
            {inc.acknowledged_at ? (
              <div className="sentinel-acked">
                Acknowledged{" "}
                {new Date(inc.acknowledged_at).toLocaleString()}
              </div>
            ) : (
              <button
                className="sentinel-ack"
                onClick={() => void handleAck(inc.id)}
              >
                Acknowledge
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
