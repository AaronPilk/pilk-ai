import { useEffect, useState } from "react";
import {
  fetchCostSummary,
  pilk,
  useConnection,
  type CostSummary,
} from "../state/api";
import PttButton from "./PttButton";

export default function TopBar() {
  const { status } = useConnection();
  const [running, setRunning] = useState(0);
  const [summary, setSummary] = useState<CostSummary | null>(null);

  useEffect(() => {
    fetchCostSummary().then(setSummary).catch(() => {});
    return pilk.onMessage((m) => {
      if (m.type === "plan.created") setRunning((n) => n + 1);
      else if (m.type === "plan.completed") setRunning((n) => Math.max(0, n - 1));
      else if (m.type === "cost.updated") {
        fetchCostSummary().then(setSummary).catch(() => {});
      } else if (m.type === "system.hello" && m.running_plan_id) {
        setRunning(1);
      }
    });
  }, []);

  const connClass =
    status === "open"
      ? "topbar-conn topbar-conn--ok"
      : status === "connecting"
        ? "topbar-conn topbar-conn--warn"
        : "topbar-conn topbar-conn--bad";

  return (
    <header className="topbar">
      <div className="topbar-left">
        <span className={connClass}>
          <span className="topbar-conn-dot" />
          <span className="topbar-conn-label">PILKD</span>
          <span className="topbar-conn-state">{status}</span>
        </span>
      </div>
      <div className="topbar-right">
        <div className="topbar-stats">
          <div className="topbar-stat">
            <span className="topbar-stat-label">Running</span>
            <span className="topbar-stat-value">{running}</span>
          </div>
          <div className="topbar-stat">
            <span className="topbar-stat-label">Today</span>
            <span className="topbar-stat-value">
              ${summary ? summary.day_usd.toFixed(4) : "0.0000"}
            </span>
          </div>
        </div>
        <PttButton />
      </div>
    </header>
  );
}
