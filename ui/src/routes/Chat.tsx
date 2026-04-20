import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { pilk, type ApprovalRequest } from "../state/api";
import { useLivePlans } from "../state/plans";
import PlanCard from "../components/PlanCard";
import ApprovalInline from "../components/ApprovalInline";
import VoiceOrb from "../components/VoiceOrb";

type Msg =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; text: string; plan_id?: string }
  | { kind: "system"; id: string; text: string }
  | { kind: "plan"; plan_id: string }
  | { kind: "approval"; approval_id: string };

function uid() {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

/** Kick-off prompt for the "Let PILK get to know you" interview.
 * Deliberately chatty so PILK reads it as a user-authored request and
 * responds conversationally rather than treating it as a tool-routing
 * spec. The key instructions: one question at a time, branch on my
 * answers, save durable entries via memory_remember when I confirm. */
const INTERVIEW_KICKOFF =
  "Let's do a get-to-know-me interview so you can learn how I work, " +
  "what I care about, and what my quirks are. Rules of engagement:\n\n" +
  "• Ask one question at a time and wait for my answer before the next.\n" +
  "• Branch the next question based on what I just told you — follow " +
  "the thread rather than reading a script.\n" +
  "• Rotate topics after a few turns so you don't drill the same area: " +
  "work, goals, people + relationships, routines, preferences, quirks, " +
  "pet peeves.\n" +
  "• Every few answers, distil what you've learned into a concrete entry " +
  "and call the memory_remember tool with the right kind (preference / " +
  "standing_instruction / fact / pattern). Keep titles short and " +
  "scannable. Confirm with me before saving anything sensitive.\n" +
  "• Keep it conversational — your replies are read aloud too, so short " +
  "sentences, no bullet spam.\n\n" +
  "Start with one warm opener.";

export default function Chat() {
  const { plans } = useLivePlans();
  const [messages, setMessages] = useState<Msg[]>([]);
  const [approvals, setApprovals] = useState<Record<string, ApprovalRequest>>({});
  const [resolvedApprovals, setResolvedApprovals] = useState<Record<string, { decision: string; reason?: string }>>({});
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();

  // Prefill the composer when another surface links here with ?prompt=…
  // (e.g. the Home InboxCard "Ask PILK to triage" CTA). Strip the query
  // after capture so reload/back doesn't resubmit.
  useEffect(() => {
    const prefill = searchParams.get("prompt");
    if (!prefill) return;
    setInput((current) => (current ? current : prefill));
    const next = new URLSearchParams(searchParams);
    next.delete("prompt");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  // "Let PILK get to know you" CTA on /memory links here with
  // ?start=interview. We auto-submit a kick-off prompt that tells
  // PILK to run a conversational onboarding and save answers via the
  // memory_remember tool.
  useEffect(() => {
    const mode = searchParams.get("start");
    if (mode !== "interview") return;
    const id = uid();
    const text = INTERVIEW_KICKOFF;
    setMessages((prev) => [...prev, { kind: "user", id, text }]);
    pilk.send({ type: "chat.user", id, text });
    const next = new URLSearchParams(searchParams);
    next.delete("start");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

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
            <div className="chat-empty-line">
              Tell me what you want done — I'll execute.
            </div>
            <div className="chat-empty-line chat-empty-line--soft">
              Say "Hey PILK" when ambient listening is on, tap the orb, or type
              below. Anything risky pauses for your approval inline.
            </div>
            <div className="chat-empty-suggest">
              <span className="chat-empty-suggest-pill">
                "Open a browser and visit example.com"
              </span>
              <span className="chat-empty-suggest-pill">
                "Build me a sales outreach agent"
              </span>
              <span className="chat-empty-suggest-pill">
                "Summarize my downloads folder"
              </span>
            </div>
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
                  className={`msg msg--closed msg--closed-${resolved.decision}`}
                >
                  <div className="msg-text">
                    <strong>
                      Approval {resolved.decision}
                      {resolved.reason ? " · " : ""}
                    </strong>
                    {resolved.reason}
                  </div>
                </div>
              );
            }
            return null;
          }
          if (m.kind === "system") {
            return (
              <div key={m.id ?? i} className="msg msg--system">
                <div className="msg-text">{m.text}</div>
              </div>
            );
          }
          return (
            <div key={m.id ?? i} className={`msg msg--${m.kind}`}>
              <div className="msg-text">{m.text}</div>
            </div>
          );
        })}
        <div ref={endRef} />
      </div>
      <div className="chat-orb-dock">
        <VoiceOrb size="large" />
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
          rows={3}
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
