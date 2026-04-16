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

  const dot =
    status === "open" ? "#4fbf7a" : status === "connecting" ? "#e0b84a" : "#e55a5a";
  return (
    <header className="topbar">
      <div className="topbar-left">
        <span className="topbar-conn">
          <span className="dot" style={{ background: dot }} />
          pilkd · {status}
        </span>
      </div>
      <div className="topbar-right">
        <PttButton />
        <span className="topbar-stat">running {running}</span>
        <span className="topbar-stat">
          today ${summary ? summary.day_usd.toFixed(4) : "0.0000"}
        </span>
      </div>
    </header>
  );
}
