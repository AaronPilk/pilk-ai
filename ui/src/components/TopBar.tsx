import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  cancelAllRunning,
  fetchCostSummary,
  pilk,
  useConnection,
  type CostSummary,
} from "../state/api";
import { isCloudMode } from "../lib/supabase";
import { signOutAndReturnToPortal } from "../lib/AuthGate";
import VoiceOrb from "./VoiceOrb";

export default function TopBar() {
  const { status } = useConnection();
  const { pathname } = useLocation();
  const [running, setRunning] = useState(0);
  const [summary, setSummary] = useState<CostSummary | null>(null);
  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    fetchCostSummary().then(setSummary).catch(() => {});
    return pilk.onMessage((m) => {
      if (m.type === "plan.created") setRunning((n) => n + 1);
      else if (m.type === "plan.completed") {
        setRunning((n) => Math.max(0, n - 1));
        setStopping(false);
      } else if (m.type === "cost.updated") {
        fetchCostSummary().then(setSummary).catch(() => {});
      } else if (m.type === "system.hello" && m.running_plan_id) {
        setRunning(1);
      }
    });
  }, []);

  const handleStopAll = async () => {
    setStopping(true);
    try {
      await cancelAllRunning();
    } catch {
      setStopping(false);
    }
  };

  const connClass =
    status === "open"
      ? "topbar-conn topbar-conn--ok"
      : status === "connecting"
        ? "topbar-conn topbar-conn--warn"
        : "topbar-conn topbar-conn--bad";

  // The Home and Chat routes host their own large orb, so the topbar orb hides there.
  const hasLargeOrb = pathname === "/" || pathname === "/chat";

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
        {running > 0 && (
          <button
            className="topbar-stopall"
            onClick={() => void handleStopAll()}
            disabled={stopping}
            title="Emergency stop — cancels the running plan and closes every live browser session."
          >
            {stopping ? "Stopping…" : "Stop all"}
          </button>
        )}
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
        {!hasLargeOrb && (
          <VoiceOrb size="small" showLabel={false} showCaption={false} />
        )}
        {isCloudMode && (
          <button
            className="topbar-signout"
            onClick={() => void signOutAndReturnToPortal()}
            title="Sign out and return to pilk.ai"
          >
            Sign out
          </button>
        )}
      </div>
    </header>
  );
}
