import { useState } from "react";
import {
  approveApproval,
  rejectApproval,
  type ApprovalRequest,
  type TrustScope,
} from "../state/api";

export default function ApprovalInline({ approval }: { approval: ApprovalRequest }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [remember, setRemember] = useState(false);

  const trustAllowed = !approval.bypass_trust;
  const scope: TrustScope = "agent+args";

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
        <span className={`appr-risk appr-risk--${approval.risk_class}`}>
          {approval.risk_class}
        </span>
        <span className="appr-card-tool">{approval.tool_name}</span>
        {approval.agent_name && (
          <span className="appr-card-agent">{approval.agent_name}</span>
        )}
      </div>
      <div className="appr-inline-reason">{approval.reason}</div>
      <pre className="step-io appr-args">
        {JSON.stringify(approval.args, null, 2)}
      </pre>
      <div className="appr-inline-actions">
        {trustAllowed ? (
          <label className="appr-inline-remember">
            <input
              type="checkbox"
              checked={remember}
              onChange={(e) => setRemember(e.target.checked)}
              disabled={busy}
            />
            remember for 30 min
          </label>
        ) : (
          <span className="appr-inline-lock">financial — trust disabled</span>
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
