import { useCallback, useEffect, useRef, useState } from "react";
import { pilk, type ApprovalRequest } from "../state/api";
import { useLivePlans } from "../state/plans";
import PlanCard from "../components/PlanCard";
import ApprovalInline from "../components/ApprovalInline";

type Msg =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; text: string; plan_id?: string }
  | { kind: "system"; id: string; text: string }
  | { kind: "plan"; plan_id: string }
  | { kind: "approval"; approval_id: string };

function uid() {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export default function Chat() {
  const { plans } = useLivePlans();
  const [messages, setMessages] = useState<Msg[]>([]);
  const [approvals, setApprovals] = useState<Record<string, ApprovalRequest>>({});
  const [resolvedApprovals, setResolvedApprovals] = useState<Record<string, { decision: string; reason?: string }>>({});
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    return pilk.onMessage((m) => {
      if (m.type === "plan.created") {
        setBusy(true);
        setMessages((prev) => [...prev, { kind: "plan", plan_id: m.id }]);
      } else if (m.type === "plan.completed") {
        setBusy(false);
      } else if (m.type === "chat.assistant") {
        setMessages((prev) => [
          ...prev,
          {
            kind: "assistant",
            id: uid(),
            text: m.text ?? "",
            plan_id: m.plan_id,
          },
        ]);
      } else if (m.type === "system.hello") {
        setMessages((prev) => [
          ...prev,
          { kind: "system", id: uid(), text: m.text ?? "connected" },
        ]);
      } else if (m.type === "system.error") {
        setMessages((prev) => [
          ...prev,
          { kind: "system", id: uid(), text: `error: ${m.text ?? ""}` },
        ]);
        setBusy(false);
      } else if (m.type === "approval.created") {
        const req = m as ApprovalRequest & { type: string };
        setApprovals((prev) => ({ ...prev, [req.id]: req }));
        setMessages((prev) => [
          ...prev,
          { kind: "approval", approval_id: req.id },
        ]);
      } else if (m.type === "approval.resolved") {
        setResolvedApprovals((prev) => ({
          ...prev,
          [m.id]: { decision: m.decision, reason: m.reason },
        }));
        setApprovals((prev) => {
          if (!(m.id in prev)) return prev;
          const next = { ...prev };
          delete next[m.id];
          return next;
        });
      }
    });
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, plans, approvals]);

  const submit = useCallback(() => {
    const text = input.trim();
    if (!text || busy) return;
    const id = uid();
    setMessages((prev) => [...prev, { kind: "user", id, text }]);
    pilk.send({ type: "chat.user", id, text });
    setInput("");
  }, [input, busy]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="chat">
      <div className="chat-thread">
        {messages.length === 0 && (
          <div className="chat-empty">
            Give PILK a goal and it will plan, execute, and report back.
            <br />
            ⌘/Ctrl+Enter to send. Tools available this batch: fs_read, fs_write,
            shell_exec, llm_ask, net_fetch, finance_*. Network, financial, and
            comms calls pause for approval inline.
          </div>
        )}
        {messages.map((m, i) => {
          if (m.kind === "plan") {
            const plan = plans[m.plan_id];
            return plan ? <PlanCard key={`plan-${m.plan_id}`} plan={plan} /> : null;
          }
          if (m.kind === "approval") {
            const pending = approvals[m.approval_id];
            if (pending) {
              return <ApprovalInline key={`appr-${m.approval_id}`} approval={pending} />;
            }
            const resolved = resolvedApprovals[m.approval_id];
            if (resolved) {
              return (
                <div
                  key={`appr-${m.approval_id}`}
                  className={`msg msg--system appr-inline-closed appr-inline-closed--${resolved.decision}`}
                >
                  <div className="msg-role">approval · {resolved.decision}</div>
                  {resolved.reason && (
                    <div className="msg-text">{resolved.reason}</div>
                  )}
                </div>
              );
            }
            return null;
          }
          return (
            <div key={m.id ?? i} className={`msg msg--${m.kind}`}>
              <div className="msg-role">{m.kind}</div>
              <div className="msg-text">{m.text}</div>
            </div>
          );
        })}
        <div ref={endRef} />
      </div>
      <div className="chat-composer">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={
            busy
              ? "A plan is currently running — wait for it to finish…"
              : "Tell PILK what to do. ⌘/Ctrl+Enter to send."
          }
          rows={4}
          disabled={busy}
        />
        <div className="chat-actions">
          <button
            className="btn btn--primary"
            onClick={submit}
            disabled={busy || !input.trim()}
          >
            {busy ? "Running…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}
