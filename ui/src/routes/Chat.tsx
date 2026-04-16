import { useCallback, useEffect, useRef, useState } from "react";
import { pilk } from "../state/api";

type Msg = {
  id: string;
  role: "user" | "pilk" | "system";
  text: string;
};

function uid() {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

export default function Chat() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    return pilk.onMessage((m) => {
      if (m.type === "chat.reply") {
        setMessages((prev) => [
          ...prev,
          { id: m.id ?? uid(), role: "pilk", text: m.text ?? "" },
        ]);
      } else if (m.type === "system.hello") {
        setMessages((prev) => [
          ...prev,
          { id: m.id ?? uid(), role: "system", text: m.text ?? "" },
        ]);
      } else if (m.type === "system.error") {
        setMessages((prev) => [
          ...prev,
          { id: uid(), role: "system", text: `error: ${m.text ?? ""}` },
        ]);
      }
    });
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const submit = useCallback(() => {
    const text = input.trim();
    if (!text) return;
    const id = uid();
    setMessages((prev) => [...prev, { id, role: "user", text }]);
    pilk.send({ type: "chat.user", id, text });
    setInput("");
  }, [input]);

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
            Say something. ⌘/Ctrl+Enter to send.
            <br />
            Batch 0 just echoes — the orchestrator lands in batch 1.
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`msg msg--${m.role}`}>
            <div className="msg-role">{m.role}</div>
            <div className="msg-text">{m.text}</div>
          </div>
        ))}
        <div ref={endRef} />
      </div>
      <div className="chat-composer">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Type a message. ⌘/Ctrl+Enter to send."
          rows={4}
        />
        <div className="chat-actions">
          <button className="btn btn--primary" onClick={submit}>
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
