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

type TtlOption = { label: string; seconds: number };
const TTL_OPTIONS: TtlOption[] = [
  { label: "once", seconds: 0 },
  { label: "5 min", seconds: 5 * 60 },
  { label: "30 min", seconds: 30 * 60 },
  { label: "2 hours", seconds: 2 * 60 * 60 },
];

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

  return (
    <div className="approvals">
      <div className="approvals-head">
        <h2>Approvals</h2>
        {pending.length > 0 && (
          <button
            className="btn"
            onClick={onApproveAll}
            disabled={busy}
            title="Approves every pending item except financial calls"
          >
            Approve all (except financial)
          </button>
        )}
      </div>
      {flash && <div className="agent-flash">{flash}</div>}

      <section className="approvals-section">
        <h3>Pending</h3>
        {pending.length === 0 ? (
          <div className="tasks-empty">Nothing waiting on you.</div>
        ) : (
          pending.map((a) => (
            <PendingCard
              key={a.id}
              approval={a}
              onApprove={onApprove}
              onReject={onReject}
              disabled={busy}
            />
          ))
        )}
      </section>

      <section className="approvals-section">
        <h3>Live trust rules</h3>
        {trust.length === 0 ? (
          <div className="tasks-empty">No active trust rules.</div>
        ) : (
          <TrustList rules={trust} onRevoke={async (id) => {
            try {
              await revokeTrust(id);
            } catch (e: any) {
              setFlash(`Error: ${e?.message ?? e}`);
            }
          }} />
        )}
      </section>

      <section className="approvals-section">
        <h3>Recent</h3>
        {recent.length === 0 ? (
          <div className="tasks-empty">No decisions yet.</div>
        ) : (
          <RecentList rows={recent} />
        )}
      </section>
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
    <div className="appr-card">
      <div className="appr-card-head">
        <span className="appr-card-tool" title={approval.tool_name}>
          {humanizeToolName(approval.tool_name)}
        </span>
        <span
          className={`appr-risk appr-risk--${approval.risk_class}`}
          title={approval.risk_class}
        >
          {humanizeRiskClass(approval.risk_class)}
        </span>
        {approval.agent_name && (
          <span className="appr-card-agent" title={approval.agent_name}>
            {humanizeAgentName(approval.agent_name)}
          </span>
        )}
      </div>
      <div className="appr-card-reason">{approval.reason}</div>
      <pre className="step-io appr-args">
        {JSON.stringify(approval.args, null, 2)}
      </pre>
      <div className="appr-actions">
        {trustAllowed ? (
          <div className="appr-trust">
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
          <div className="appr-trust appr-trust--locked">
            {approval.tool_name === "agent_create"
              ? "system change — trust rules disabled"
              : approval.risk_class === "FINANCIAL"
                ? "financial — trust rules disabled"
                : "trust rules disabled"}
          </div>
        )}
        <div className="appr-buttons">
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

function TrustList({
  rules,
  onRevoke,
}: {
  rules: TrustRule[];
  onRevoke: (id: string) => void;
}) {
  return (
    <table className="cost-table">
      <thead>
        <tr>
          <th>Tool</th>
          <th>Agent</th>
          <th>Matcher</th>
          <th>Uses</th>
          <th>Expires in</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {rules.map((r) => (
          <tr key={r.id}>
            <td title={r.tool_name}>{humanizeToolName(r.tool_name)}</td>
            <td>{r.agent_name ? humanizeAgentName(r.agent_name) : "—"}</td>
            <td className="cost-table-plan">
              {Object.keys(r.args_matcher).length === 0
                ? "Any arguments"
                : JSON.stringify(r.args_matcher)}
            </td>
            <td>{r.uses}</td>
            <td>{formatTtl(r.expires_in_s)}</td>
            <td>
              <button className="btn" onClick={() => onRevoke(r.id)}>
                Revoke
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RecentList({ rows }: { rows: ApprovalHistoryRow[] }) {
  const visible = useMemo(
    () => rows.filter((r) => r.status !== "pending"),
    [rows],
  );
  if (visible.length === 0) return <div className="tasks-empty">No decisions yet.</div>;
  return (
    <table className="cost-table">
      <thead>
        <tr>
          <th>Tool</th>
          <th>Risk</th>
          <th>Status</th>
          <th>Agent</th>
          <th>Decided</th>
        </tr>
      </thead>
      <tbody>
        {visible.map((r) => (
          <tr key={r.id}>
            <td title={r.tool}>{humanizeToolName(r.tool)}</td>
            <td>
              <span
                className={`appr-risk appr-risk--${r.risk_class}`}
                title={r.risk_class}
              >
                {humanizeRiskClass(r.risk_class)}
              </span>
            </td>
            <td>
              <span className={`tasks-row-status tasks-row-status--${r.status}`}>
                {capitalize(r.status)}
              </span>
            </td>
            <td>{r.agent_name ? humanizeAgentName(r.agent_name) : "—"}</td>
            <td>{r.decided_at ? new Date(r.decided_at).toLocaleString() : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function formatTtl(seconds: number): string {
  if (seconds <= 0) return "expired";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function capitalize(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}
