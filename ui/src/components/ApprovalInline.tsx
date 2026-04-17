import { useState } from "react";
import {
  approveApproval,
  rejectApproval,
  type ApprovalRequest,
  type TrustScope,
} from "../state/api";
import {
  humanizeAgentName,
  humanizeRiskClass,
  humanizeToolName,
} from "../lib/humanize";

export default function ApprovalInline({ approval }: { approval: ApprovalRequest }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [remember, setRemember] = useState(false);
  const [showArgs, setShowArgs] = useState(false);

  const trustAllowed = !approval.bypass_trust;
  const scope: TrustScope = "agent+args";
  const lockReason = describeLock(approval);
  const hasArgs =
    approval.args &&
    typeof approval.args === "object" &&
    Object.keys(approval.args as object).length > 0;

  const act = async (kind: "approve" | "reject") => {
    setBusy(true);
    setErr(null);
    try {
      if (kind === "approve") {
        await approveApproval(approval.id, {
          trust:
            remember && trustAllowed
              ? { scope, ttl_seconds: 30 * 60 }
              : undefined,
        });
      } else {
        await rejectApproval(approval.id, {});
      }
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="appr-inline">
      <div className="appr-inline-head">
        <span className="appr-inline-label">Approval needed</span>
        <span
          className={`appr-risk appr-risk--${approval.risk_class}`}
          title={approval.risk_class}
        >
          {humanizeRiskClass(approval.risk_class)}
        </span>
        <span className="appr-card-tool" title={approval.tool_name}>
          {humanizeToolName(approval.tool_name)}
        </span>
        {approval.agent_name && (
          <span className="appr-card-agent" title={approval.agent_name}>
            {humanizeAgentName(approval.agent_name)}
          </span>
        )}
      </div>
      <div className="appr-inline-reason">{approval.reason}</div>

      {hasArgs && (
        <div className="appr-args-wrap">
          <button
            type="button"
            className="appr-args-toggle"
            onClick={() => setShowArgs((s) => !s)}
            aria-expanded={showArgs}
          >
            {showArgs ? "Hide" : "View"} arguments
            <span className="appr-args-chev" aria-hidden>
              {showArgs ? "▾" : "▸"}
            </span>
          </button>
          {showArgs && (
            <pre className="step-io appr-args">
              {JSON.stringify(approval.args, null, 2)}
            </pre>
          )}
        </div>
      )}

      <div className="appr-inline-actions">
        {trustAllowed ? (
          <label className="appr-inline-remember">
            <input
              type="checkbox"
              checked={remember}
              onChange={(e) => setRemember(e.target.checked)}
              disabled={busy}
            />
            Remember for 30 min
          </label>
        ) : (
          <span className="appr-inline-lock">{lockReason}</span>
        )}
        <div className="appr-buttons">
          <button className="btn" onClick={() => act("reject")} disabled={busy}>
            Reject
          </button>
          <button
            className="btn btn--primary"
            onClick={() => act("approve")}
            disabled={busy}
          >
            Approve
          </button>
        </div>
      </div>
      {err && <div className="step-error">{err}</div>}
    </div>
  );
}

function describeLock(a: ApprovalRequest): string {
  if (a.tool_name === "agent_create") return "System change — trust disabled";
  if (a.risk_class === "FINANCIAL") return "Financial — trust disabled";
  return "Trust disabled";
}
