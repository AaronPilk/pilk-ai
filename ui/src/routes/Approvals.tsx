import { useCallback, useEffect, useMemo, useState } from "react";
import {
  approveAllPending,
  approveApproval,
  fetchApprovals,
  fetchTrust,
  pilk,
  rejectApproval,
  revokeTrust,
  type ApprovalHistoryRow,
  type ApprovalRequest,
  type TrustRule,
  type TrustScope,
} from "../state/api";
import {
  humanizeAgentName,
  humanizeRiskClass,
  humanizeToolName,
} from "../lib/humanize";

/** Springboard-style Approvals. Matches the /agents and /tasks
 * visual language: a page-level intro, then card galleries for
 * Pending (actionable) / Trust (live rules) / Recent (history).
 * No admin-table shape; every item is its own card. */

type TtlOption = { label: string; seconds: number };
const TTL_OPTIONS: TtlOption[] = [
  { label: "just once", seconds: 0 },
  { label: "5 min", seconds: 5 * 60 },
  { label: "30 min", seconds: 30 * 60 },
  { label: "2 hours", seconds: 2 * 60 * 60 },
];

const STATUS_ICON: Record<string, string> = {
  approved: "✓",
  rejected: "✕",
  expired: "⊘",
  pending: "⋯",
};

function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 45) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d} d ago`;
  return new Date(iso).toLocaleDateString();
}

export default function Approvals() {
  const [pending, setPending] = useState<ApprovalRequest[]>([]);
  const [recent, setRecent] = useState<ApprovalHistoryRow[]>([]);
  const [trust, setTrust] = useState<TrustRule[]>([]);
  const [flash, setFlash] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [a, t] = await Promise.all([fetchApprovals(), fetchTrust()]);
      setPending(a.pending);
      setRecent(a.recent);
      setTrust(t.rules);
    } catch {}
  }, []);

  useEffect(() => {
    refresh();
    return pilk.onMessage((m) => {
      if (
        m.type === "approval.created" ||
        m.type === "approval.resolved" ||
        m.type === "trust.updated" ||
        m.type === "trust.revoked"
      ) {
        refresh();
      }
    });
  }, [refresh]);

  const onApprove = async (
    id: string,
    scope: TrustScope,
    ttl: number,
  ) => {
    setBusy(true);
    setFlash(null);
    try {
      await approveApproval(id, {
        reason: "",
        trust: ttl > 0 ? { scope, ttl_seconds: ttl } : undefined,
      });
    } catch (e: any) {
      setFlash(`Error: ${e?.message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  const onReject = async (id: string) => {
    setBusy(true);
    setFlash(null);
    try {
      await rejectApproval(id, { reason: "" });
    } catch (e: any) {
      setFlash(`Error: ${e?.message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  const onApproveAll = async () => {
    if (pending.length === 0) return;
    setBusy(true);
    setFlash(null);
    try {
      const r = await approveAllPending();
      setFlash(
        r.count === pending.length
          ? `Approved all ${r.count}.`
          : `Approved ${r.count}; ${pending.length - r.count} financial call(s) still need a fresh decision.`,
      );
    } catch (e: any) {
      setFlash(`Error: ${e?.message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  const recentVisible = useMemo(
    () => recent.filter((r) => r.status !== "pending"),
    [recent],
  );

  return (
    <div className="agents-page">
      <div className="approvals-page-head">
        <div className="agents-page-head">
          <h1>Approvals</h1>
          <p>
            Things PILK wants to do that need your sign-off, the trust
            rules keeping similar calls running without prompting, and
            a short history of recent decisions.
          </p>
        </div>
        {pending.length > 0 && (
          <button
            className="btn btn--primary"
            onClick={onApproveAll}
            disabled={busy}
            title="Approves every pending item except financial calls"
          >
            Approve all (except financial)
          </button>
        )}
      </div>

      {flash && <div className="agent-flash">{flash}</div>}

      {/* ── Pending ───────────────────────────────────────────── */}
      <div className="approvals-section-head">
        <h2>Waiting on you</h2>
        {pending.length > 0 && (
          <span className="approvals-count">{pending.length}</span>
        )}
      </div>
      {pending.length === 0 ? (
        <div className="agents-empty">Nothing waiting on you.</div>
      ) : (
        <div className="agents-gallery approvals-gallery">
          {pending.map((a) => (
            <PendingCard
              key={a.id}
              approval={a}
              onApprove={onApprove}
              onReject={onReject}
              disabled={busy}
            />
          ))}
        </div>
      )}

      {/* ── Live trust rules ──────────────────────────────────── */}
      <div className="approvals-section-head">
        <h2>Live trust rules</h2>
        {trust.length > 0 && (
          <span className="approvals-count">{trust.length}</span>
        )}
      </div>
      {trust.length === 0 ? (
        <div className="agents-empty">
          No active trust rules. When you approve something and check
          "also trust for 30 min", it shows up here.
        </div>
      ) : (
        <div className="agents-gallery approvals-trust-gallery">
          {trust.map((r) => (
            <TrustCard
              key={r.id}
              rule={r}
              onRevoke={async (id) => {
                try {
                  await revokeTrust(id);
                } catch (e: any) {
                  setFlash(`Error: ${e?.message ?? e}`);
                }
              }}
            />
          ))}
        </div>
      )}

      {/* ── Recent decisions ──────────────────────────────────── */}
      <div className="approvals-section-head">
        <h2>Recent decisions</h2>
      </div>
      {recentVisible.length === 0 ? (
        <div className="agents-empty">No decisions yet.</div>
      ) : (
        <div className="agents-gallery tasks-gallery">
          {recentVisible.map((r) => (
            <RecentCard key={r.id} row={r} />
          ))}
        </div>
      )}
    </div>
  );
}

function PendingCard({
  approval,
  onApprove,
  onReject,
  disabled,
}: {
  approval: ApprovalRequest;
  onApprove: (id: string, scope: TrustScope, ttl: number) => void;
  onReject: (id: string) => void;
  disabled: boolean;
}) {
  const [scope, setScope] = useState<TrustScope>("agent+args");
  const [ttl, setTtl] = useState<number>(0);
  const trustAllowed = !approval.bypass_trust;

  return (
    <div className="agent-card approvals-pending-card">
      <div className="approvals-pending-head">
        <div className="approvals-pending-tool" title={approval.tool_name}>
          {humanizeToolName(approval.tool_name)}
        </div>
        <span
          className={`appr-risk appr-risk--${approval.risk_class}`}
          title={approval.risk_class}
        >
          {humanizeRiskClass(approval.risk_class)}
        </span>
      </div>
      {approval.agent_name && (
        <div className="approvals-pending-agent">
          {humanizeAgentName(approval.agent_name)}
          <span className="approvals-pending-time">
            · {timeAgo(approval.created_at)}
          </span>
        </div>
      )}
      <div className="approvals-pending-reason">{approval.reason}</div>
      <pre className="approvals-pending-args">
        {JSON.stringify(approval.args, null, 2)}
      </pre>
      <div className="approvals-pending-actions">
        {trustAllowed ? (
          <div className="approvals-pending-trust">
            <label>also trust for</label>
            <select
              value={ttl}
              onChange={(e) => setTtl(Number(e.target.value))}
              disabled={disabled}
            >
              {TTL_OPTIONS.map((o) => (
                <option key={o.seconds} value={o.seconds}>
                  {o.label}
                </option>
              ))}
            </select>
            {ttl > 0 && (
              <select
                value={scope}
                onChange={(e) => setScope(e.target.value as TrustScope)}
                disabled={disabled}
              >
                <option value="agent+args">exact args</option>
                <option value="agent">any args</option>
              </select>
            )}
          </div>
        ) : (
          <div className="approvals-pending-trust approvals-pending-trust--locked">
            {approval.tool_name === "agent_create"
              ? "system change — trust rules disabled"
              : approval.risk_class === "FINANCIAL"
                ? "financial — trust rules disabled"
                : "trust rules disabled"}
          </div>
        )}
        <div className="approvals-pending-buttons">
          <button
            className="btn"
            onClick={() => onReject(approval.id)}
            disabled={disabled}
          >
            Reject
          </button>
          <button
            className="btn btn--primary"
            onClick={() => onApprove(approval.id, scope, ttl)}
            disabled={disabled}
          >
            Approve
          </button>
        </div>
      </div>
    </div>
  );
}

function TrustCard({
  rule,
  onRevoke,
}: {
  rule: TrustRule;
  onRevoke: (id: string) => void;
}) {
  const matcher =
    Object.keys(rule.args_matcher).length === 0
      ? "Any arguments"
      : JSON.stringify(rule.args_matcher);
  return (
    <div className="agent-card approvals-trust-card">
      <div className="agent-card-avatar" aria-hidden>
        🛂
      </div>
      <div className="agent-card-body">
        <div className="agent-card-name">{humanizeToolName(rule.tool_name)}</div>
        <div className="agent-card-blurb">
          {rule.agent_name ? (
            <>
              via <strong>{humanizeAgentName(rule.agent_name)}</strong>
              <br />
            </>
          ) : null}
          <span className="approvals-trust-matcher">{matcher}</span>
        </div>
        <div className="agent-card-meta">
          <span className="agent-card-status agent-card-status--ready">
            <span className="agent-card-status-dot" />
            {formatTtl(rule.expires_in_s)}
          </span>
          <span className="agent-card-autonomy">
            {rule.uses} {rule.uses === 1 ? "use" : "uses"}
          </span>
          <button
            className="approvals-trust-revoke"
            onClick={() => onRevoke(rule.id)}
          >
            Revoke
          </button>
        </div>
      </div>
    </div>
  );
}

function RecentCard({ row }: { row: ApprovalHistoryRow }) {
  const icon = STATUS_ICON[row.status] ?? "?";
  return (
    <div className={`agent-card task-card task-card--${row.status}`}>
      <div className="task-card-head">
        <span
          className={`task-card-icon task-card-icon--${row.status}`}
          aria-hidden
        >
          {icon}
        </span>
        <span className="task-card-time">
          {row.decided_at ? timeAgo(row.decided_at) : timeAgo(row.created_at)}
        </span>
      </div>
      <div className="task-card-goal">
        {humanizeToolName(row.tool)}
        {row.agent_name && (
          <span className="task-card-more">
            {" "}
            · {humanizeAgentName(row.agent_name)}
          </span>
        )}
      </div>
      <div className="task-card-footer">
        <span className={`appr-risk appr-risk--${row.risk_class}`}>
          {humanizeRiskClass(row.risk_class)}
        </span>
        <span
          className={`agent-card-status agent-card-status--${row.status}`}
        >
          <span className="agent-card-status-dot" />
          {capitalize(row.status)}
        </span>
      </div>
    </div>
  );
}

function formatTtl(seconds: number): string {
  if (seconds <= 0) return "expired";
  if (seconds < 60) return `${seconds}s left`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m left`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m left`;
}

function capitalize(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}
