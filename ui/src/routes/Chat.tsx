import { useCallback, useEffect, useRef, useState } from "react";
import { pilk } from "../state/api";
import { useLivePlans } from "../state/plans";
import PlanCard from "../components/PlanCard";

type Msg =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; text: string; plan_id?: string }
  | { kind: "system"; id: string; text: string }
  | { kind: "plan"; plan_id: string };

function uid() {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export default function Chat() {
  const { plans } = useLivePlans();
  const [messages, setMessages] = useState<Msg[]>([]);
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
      }
    });
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, plans]);

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
            ⌘/Ctrl+Enter to send. Tools available this batch: fs.read, fs.write,
            shell.exec, llm.ask — all scoped to <code>~/PILK/workspace/</code>.
          </div>
        )}
        {messages.map((m, i) => {
          if (m.kind === "plan") {
            const plan = plans[m.plan_id];
            return plan ? <PlanCard key={`plan-${m.plan_id}`} plan={plan} /> : null;
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
