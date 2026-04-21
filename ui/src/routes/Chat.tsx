import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  deleteChatAttachment,
  pilk,
  uploadChatAttachment,
  type ApprovalRequest,
  type ChatAttachment,
} from "../state/api";
import { useLivePlans } from "../state/plans";
import PlanCard from "../components/PlanCard";
import ApprovalInline from "../components/ApprovalInline";
import VoiceOrb from "../components/VoiceOrb";

type Msg =
  | { kind: "user"; id: string; text: string; attachments?: ChatAttachment[] }
  | { kind: "assistant"; id: string; text: string; plan_id?: string }
  | { kind: "system"; id: string; text: string }
  | { kind: "plan"; plan_id: string }
  | { kind: "approval"; approval_id: string };

/** Files the composer already uploaded; kept local to the draft until
 * Send wires them onto the WS message. Each entry pairs a ChatAttachment
 * with a preview URL for image thumbnails (freed on remove / submit). */
interface DraftAttachment {
  attachment: ChatAttachment;
  previewUrl?: string;
}

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
  const [drafts, setDrafts] = useState<DraftAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
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

  const uploadFiles = useCallback(async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (list.length === 0) return;
    setUploading(true);
    setUploadError(null);
    // Upload in sequence rather than parallel — keeps the browser's
    // progress feedback legible and avoids the server choking on five
    // 20 MiB PDFs arriving on the same event-loop tick.
    for (const file of list) {
      try {
        const attachment = await uploadChatAttachment(file);
        const previewUrl = file.type.startsWith("image/")
          ? URL.createObjectURL(file)
          : undefined;
        setDrafts((prev) => [...prev, { attachment, previewUrl }]);
      } catch (e) {
        setUploadError(e instanceof Error ? e.message : String(e));
        break;
      }
    }
    setUploading(false);
  }, []);

  const removeDraft = useCallback(
    (id: string) => {
      setDrafts((prev) => {
        const match = prev.find((d) => d.attachment.id === id);
        if (match?.previewUrl) URL.revokeObjectURL(match.previewUrl);
        return prev.filter((d) => d.attachment.id !== id);
      });
      // Fire-and-forget server-side cleanup. The orchestrator would
      // only read the file if referenced on a send, so leaving it on
      // disk is harmless — this just keeps temp clean.
      void deleteChatAttachment(id);
    },
    [],
  );

  const submit = useCallback(() => {
    const text = input.trim();
    if (busy) return;
    if (!text && drafts.length === 0) return;
    const id = uid();
    const attachments = drafts.map((d) => d.attachment);
    setMessages((prev) => [
      ...prev,
      {
        kind: "user",
        id,
        text,
        attachments: attachments.length ? attachments : undefined,
      },
    ]);
    pilk.send({
      type: "chat.user",
      id,
      text,
      attachments: attachments.map((a) => ({ id: a.id })),
    });
    // Release preview ObjectURLs now that the thumbnails render from
    // the attachment metadata instead of the draft.
    drafts.forEach((d) => d.previewUrl && URL.revokeObjectURL(d.previewUrl));
    setInput("");
    setDrafts([]);
    setUploadError(null);
  }, [input, busy, drafts]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
    }
  };

  const onDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDragOver(false);
      if (busy) return;
      if (e.dataTransfer.files?.length) {
        void uploadFiles(e.dataTransfer.files);
      }
    },
    [busy, uploadFiles],
  );

  const onDragOver = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      // Needed so the browser treats this as a drop target.
      e.preventDefault();
      if (!busy) setDragOver(true);
    },
    [busy],
  );

  const onDragLeave = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      // Ignore drag-leaves that fire when hovering over an inner
      // element (e.g. textarea) — only reset when the pointer actually
      // leaves the drop container.
      if (e.currentTarget === e.target) setDragOver(false);
    },
    [],
  );

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
              {m.kind === "user" && m.attachments?.length ? (
                <div className="msg-attachments">
                  {m.attachments.map((a) => (
                    <AttachmentPreview key={a.id} attachment={a} />
                  ))}
                </div>
              ) : null}
              {m.text ? <div className="msg-text">{m.text}</div> : null}
            </div>
          );
        })}
        <div ref={endRef} />
      </div>
      <div className="chat-orb-dock">
        <VoiceOrb size="large" />
      </div>
      <div
        className={
          "chat-composer" + (dragOver ? " chat-composer--dragover" : "")
        }
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
      >
        {drafts.length > 0 && (
          <div className="chat-drafts">
            {drafts.map((d) => (
              <div key={d.attachment.id} className="chat-draft">
                {d.previewUrl ? (
                  <img
                    src={d.previewUrl}
                    alt={d.attachment.filename}
                    className="chat-draft-thumb"
                  />
                ) : (
                  <div className="chat-draft-icon">
                    {d.attachment.kind === "document" ? "PDF" : "TXT"}
                  </div>
                )}
                <div className="chat-draft-meta">
                  <div className="chat-draft-name" title={d.attachment.filename}>
                    {d.attachment.filename}
                  </div>
                  <div className="chat-draft-size">
                    {formatBytes(d.attachment.size)}
                  </div>
                </div>
                <button
                  type="button"
                  className="chat-draft-remove"
                  onClick={() => removeDraft(d.attachment.id)}
                  title="Remove attachment"
                  aria-label="Remove attachment"
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        )}
        {uploadError && (
          <div className="chat-upload-error">{uploadError}</div>
        )}
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={
            busy
              ? "A plan is currently running — wait for it to finish…"
              : "Tell PILK what to do. ⌘/Ctrl+Enter to send. Drop files anywhere."
          }
          rows={3}
          disabled={busy}
        />
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept="image/png,image/jpeg,image/gif,image/webp,application/pdf,text/plain,text/markdown,text/csv,application/json,.md,.txt,.csv,.json"
          style={{ display: "none" }}
          onChange={(e) => {
            if (e.target.files) void uploadFiles(e.target.files);
            // Reset so selecting the same file twice still fires change.
            e.target.value = "";
          }}
        />
        <div className="chat-actions">
          <button
            className="btn"
            onClick={() => fileInputRef.current?.click()}
            disabled={busy || uploading}
            title="Attach files"
          >
            {uploading ? "Uploading…" : "Attach"}
          </button>
          <button
            className="btn btn--primary"
            onClick={submit}
            disabled={
              busy || uploading || (!input.trim() && drafts.length === 0)
            }
          >
            {busy ? "Running…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Inline preview used in the chat thread for a sent attachment.
 *  Images render as a thumbnail at the stored /chat/uploads/{id}/raw
 *  path if we ever expose one; for now we fall back to a filename chip
 *  since the upload response doesn't include a public URL. */
function AttachmentPreview({ attachment }: { attachment: ChatAttachment }) {
  return (
    <div className="msg-attachment" title={attachment.filename}>
      <span className="msg-attachment-kind">
        {attachment.kind === "image"
          ? "IMG"
          : attachment.kind === "document"
            ? "PDF"
            : "TXT"}
      </span>
      <span className="msg-attachment-name">{attachment.filename}</span>
      <span className="msg-attachment-size">
        {formatBytes(attachment.size)}
      </span>
    </div>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
