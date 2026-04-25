import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  AlertTriangle,
  DollarSign,
  LogOut,
  Menu,
  Moon,
  Play,
  Sun,
  X,
  Zap,
} from "lucide-react";
import {
  cancelAllRunning,
  fetchCostSummary,
  fetchSentinelSummary,
  fetchSubscriptionUsage,
  pilk,
  useConnection,
  type CostSummary,
  type SubscriptionUsage,
} from "../state/api";
import { isCloudMode } from "../lib/supabase";
import { signOutAndReturnToPortal } from "../lib/AuthGate";
import { useTheme } from "../lib/theme";
import VoiceOrb from "./VoiceOrb";

// Poll interval for the Claude Max usage bar. Every 30s is plenty —
// plan turns fire a cost.updated event that forces a live refresh
// whenever usage actually changes.
const SUBSCRIPTION_USAGE_POLL_MS = 30_000;

type TopBarProps = {
  navOpen?: boolean;
  onToggleNav?: () => void;
};

export default function TopBar({ navOpen, onToggleNav }: TopBarProps = {}) {
  const { status } = useConnection();
  const { pathname } = useLocation();
  const [running, setRunning] = useState(0);
  const [summary, setSummary] = useState<CostSummary | null>(null);
  const [stopping, setStopping] = useState(false);
  const [sentinelCount, setSentinelCount] = useState(0);
  const [subUsage, setSubUsage] = useState<SubscriptionUsage | null>(null);
  const { theme, toggleTheme } = useTheme();

  useEffect(() => {
    const refreshSubscription = () =>
      fetchSubscriptionUsage().then(setSubUsage).catch(() => {});

    fetchCostSummary().then(setSummary).catch(() => {});
    fetchSentinelSummary()
      .then((s) => setSentinelCount(s.unacked_count))
      .catch(() => {});
    refreshSubscription();

    // Low-frequency heartbeat so the 5-hour window slides even when
    // there's no traffic; cost.updated below also forces a refresh
    // the moment a plan actually consumes a subscription turn.
    const interval = window.setInterval(
      refreshSubscription, SUBSCRIPTION_USAGE_POLL_MS,
    );

    const unsubscribe = pilk.onMessage((m) => {
      if (m.type === "plan.created") setRunning((n) => n + 1);
      else if (m.type === "plan.completed") {
        setRunning((n) => Math.max(0, n - 1));
        setStopping(false);
      } else if (m.type === "cost.updated") {
        fetchCostSummary().then(setSummary).catch(() => {});
        refreshSubscription();
      } else if (m.type === "system.hello" && m.running_plan_id) {
        setRunning(1);
      } else if (m.type === "sentinel.incident") {
        setSentinelCount((n) => n + 1);
      } else if (m.type === "sentinel.incident.acked") {
        setSentinelCount((n) => Math.max(0, n - 1));
      }
    });

    return () => {
      window.clearInterval(interval);
      unsubscribe();
    };
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
        {onToggleNav && (
          <button
            className="topbar-burger"
            onClick={onToggleNav}
            aria-label={navOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={!!navOpen}
            type="button"
          >
            {navOpen ? <X size={18} /> : <Menu size={18} />}
          </button>
        )}
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
            <Zap size={13} aria-hidden />
            {stopping ? "Stopping…" : "Stop all"}
          </button>
        )}
        <div className="topbar-stats">
          <div className="topbar-stat">
            <Play size={12} className="topbar-stat-icon" aria-hidden />
            <div className="topbar-stat-stack">
              <span className="topbar-stat-label">Running</span>
              <span className="topbar-stat-value">{running}</span>
            </div>
          </div>
          <div className="topbar-stat">
            <DollarSign size={12} className="topbar-stat-icon" aria-hidden />
            <div className="topbar-stat-stack">
              <span className="topbar-stat-label">Today</span>
              <span className="topbar-stat-value">
                ${summary ? summary.day_usd.toFixed(4) : "0.0000"}
              </span>
            </div>
          </div>
          {subUsage && (
            <div
              className={`topbar-stat topbar-stat--ring topbar-stat--ring-${subUsage.severity}`}
              title={formatSubscriptionTooltip(subUsage)}
            >
              <SubscriptionRing pct={subUsage.pct} />
              <div className="topbar-stat-stack">
                <span className="topbar-stat-label">Max</span>
                <span className="topbar-stat-value">
                  {subUsage.count}
                  <span className="topbar-stat-denom">
                    /{subUsage.estimated_cap}
                  </span>
                </span>
              </div>
            </div>
          )}
          {sentinelCount > 0 && (
            <Link
              to="/agents"
              className="topbar-stat topbar-stat--alert"
              title="Unacknowledged sentinel incidents — click to review."
            >
              <AlertTriangle
                size={12}
                className="topbar-stat-icon"
                aria-hidden
              />
              <div className="topbar-stat-stack">
                <span className="topbar-stat-label">Alerts</span>
                <span className="topbar-stat-value">{sentinelCount}</span>
              </div>
            </Link>
          )}
        </div>
        {!hasLargeOrb && (
          <VoiceOrb size="small" showLabel={false} showCaption={false} />
        )}
        <button
          className="topbar-theme"
          onClick={toggleTheme}
          title={
            theme === "dark"
              ? "Switch to light mode"
              : "Switch to dark mode"
          }
          aria-label={
            theme === "dark" ? "Switch to light mode" : "Switch to dark mode"
          }
        >
          {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
        </button>
        {isCloudMode && (
          <button
            className="topbar-signout"
            onClick={() => void signOutAndReturnToPortal()}
            title="Sign out and return to pilk.ai"
          >
            <LogOut size={13} aria-hidden />
            Sign out
          </button>
        )}
      </div>
    </header>
  );
}

function formatSubscriptionTooltip(u: SubscriptionUsage): string {
  const cc = u.claude_code_count ?? u.claude_code?.count ?? 0;
  const pilk = u.pilk_count ?? 0;
  const total = `${u.count} of ~${u.estimated_cap} Claude Max turns this 5h window.`;
  const breakdown = `  ${pilk} from PILK · ${cc} from Claude Code CLI`;
  return `${total}\n${breakdown}`;
}

// Thin SVG arc meter rendered next to the "Max" label in the top bar.
// Apple-style: a subtle track circle + a sweeping stroke that animates
// the current percentage in place. Stroke colour is driven by the
// parent's severity class so it ramps green → amber → red without
// swapping elements.
const RING_R = 9;
const RING_C = 2 * Math.PI * RING_R;

function SubscriptionRing({ pct }: { pct: number }) {
  const clamped = Math.max(0, Math.min(100, pct));
  const dash = (clamped / 100) * RING_C;
  return (
    <svg
      className="topbar-ring"
      width="22"
      height="22"
      viewBox="0 0 22 22"
      aria-hidden="true"
    >
      <circle
        className="topbar-ring-track"
        cx="11"
        cy="11"
        r={RING_R}
        fill="none"
      />
      <circle
        className="topbar-ring-fill"
        cx="11"
        cy="11"
        r={RING_R}
        fill="none"
        strokeDasharray={`${dash} ${RING_C - dash}`}
        strokeDashoffset="0"
        transform="rotate(-90 11 11)"
      />
    </svg>
  );
}
