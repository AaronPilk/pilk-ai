// Single source of truth for turning internal identifiers (agent slugs, tool
// names, risk classes, sandbox IDs, paths) into human-facing labels.
//
// Backend slugs stay untouched; every UI surface should route display strings
// through these helpers.

const ACRONYMS = new Set([
  "PILK",
  "COO",
  "AI",
  "API",
  "URL",
  "IDE",
  "CEO",
  "CTO",
  "CFO",
  "OK",
  "NPM",
  "CLI",
  "UI",
  "UX",
  "DB",
  "SQL",
  "HTTP",
  "HTTPS",
  "TTS",
  "STT",
  "CDP",
  "FS",
  "LLM",
  "OS",
  "ID",
  "USD",
  "GPT",
]);

/**
 * Turn "file_organization_agent" → "File Organization Agent".
 * Preserves a small set of acronyms and strips extra whitespace.
 */
export function humanize(raw: string | null | undefined): string {
  if (!raw) return "";
  const words = raw
    .replace(/[_\-]+/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim()
    .split(" ");
  return words
    .map((w) => {
      const up = w.toUpperCase();
      if (ACRONYMS.has(up)) return up;
      return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
    })
    .join(" ");
}

/** Agent-specific: ensure the word "Agent" shows up exactly once at the end. */
export function humanizeAgentName(raw: string | null | undefined): string {
  const base = humanize(raw);
  if (!base) return "";
  if (/\bagent\b/i.test(base)) {
    // Collapse any lowercase "agent" to canonical "Agent".
    return base.replace(/\bagent\b/gi, "Agent");
  }
  return `${base} Agent`;
}

/** Friendly labels for builtin tool names shown in plan steps, approvals, etc. */
const TOOL_LABELS: Record<string, string> = {
  browser_session_open: "Open a browser",
  browser_navigate: "Navigate in the browser",
  browser_session_close: "Close the browser",
  fs_read: "Read a file",
  fs_write: "Write a file",
  shell_exec: "Run a shell command",
  net_fetch: "Fetch a web page",
  llm_ask: "Ask a helper model",
  agent_create: "Create an agent",
  finance_deposit: "Deposit funds",
  finance_withdraw: "Withdraw funds",
  finance_transfer: "Transfer funds",
  trade_execute: "Execute a trade",
  __premium_escalation: "Use Deep Reasoning for this task?",
  code_task: "Run a coding task",
  gmail_send_as_pilk: "Send an email from PILK",
  gmail_search_pilk_inbox: "Search PILK's inbox",
  gmail_read_pilk: "Read an email in PILK's inbox",
  gmail_thread_read_pilk: "Read a thread in PILK's inbox",
  gmail_send_as_me: "Send an email as you",
  gmail_search_my_inbox: "Search your inbox",
  gmail_read_me: "Read an email in your inbox",
  gmail_thread_read_me: "Read a thread in your inbox",
  drive_search_my_files: "Search your Drive",
  drive_read_my_file: "Read a Drive file",
  calendar_read_my_today: "Read today's calendar",
  calendar_create_my_event: "Create a calendar event",
  slack_post_as_me: "Post to Slack as you",
  linkedin_post_as_me: "Post on LinkedIn as you",
  x_post_as_me: "Post on X as you",
};

export function humanizeToolName(raw: string | null | undefined): string {
  if (!raw) return "";
  return TOOL_LABELS[raw] ?? humanize(raw);
}

/** Human phrases for agent lifecycle states — used on Home and in the Agents table. */
const AGENT_STATE_LABELS: Record<string, string> = {
  registered: "Standing by",
  ready: "Ready",
  running: "Working on it",
  paused: "Paused",
  stopped: "Off",
  errored: "Needs attention",
};

export function humanizeAgentState(raw: string | null | undefined): string {
  if (!raw) return "";
  return AGENT_STATE_LABELS[raw] ?? humanize(raw);
}

/** Friendly risk-class labels (approvals, plan detail, step metadata). */
const RISK_LABELS: Record<string, string> = {
  READ: "Reads local data",
  WRITE_LOCAL: "Writes local files",
  EXEC_LOCAL: "Runs code on your machine",
  NET_READ: "Reads from the internet",
  NET_WRITE: "Writes to the internet",
  COMMS: "Sends a message",
  FINANCIAL: "Moves money",
  IRREVERSIBLE: "Permanent change",
};

export function humanizeRiskClass(raw: string | null | undefined): string {
  if (!raw) return "";
  return RISK_LABELS[raw] ?? humanize(raw);
}

/** `sb_process_file_organization_agent_…` → `Process · File Organization Agent`. */
export function humanizeSandboxId(id: string): string {
  const m = id.match(/^sb[_-]([a-z]+)[_-](.+)$/i);
  if (!m) return id.length > 28 ? `${id.slice(0, 26)}…` : id;
  const type = humanize(m[1]);
  const rest = m[2];
  // Collapse repeated slug halves.
  const parts = rest.split(/[_\-]/);
  const half = Math.floor(parts.length / 2);
  const uniq =
    parts.length > 2 &&
    parts.slice(0, half).join("_") === parts.slice(half).join("_")
      ? parts.slice(0, half).join("_")
      : rest;
  return `${type} · ${humanizeAgentName(uniq)}`;
}

/** Collapse long filesystem paths for display; tooltip retains the full value. */
export function shortenPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  if (parts.length <= 2) return path;
  return `…/${parts.slice(-2).join("/")}`;
}

/** Compact URL for display. */
export function shortHost(url: string): string {
  try {
    const u = new URL(url);
    const p = u.pathname !== "/" ? u.pathname : "";
    return `${u.host}${p.length > 40 ? `${p.slice(0, 39)}…` : p}`;
  } catch {
    return url.length > 60 ? `${url.slice(0, 59)}…` : url;
  }
}

/** Time-of-day greeting. */
export function greetingFor(date: Date = new Date()): string {
  const h = date.getHours();
  if (h < 5) return "Working late";
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  if (h < 22) return "Good evening";
  return "Working late";
}

/** "Name <name@host>" → "Name"; falls back to the bare email or "" */
export function prettySenderName(raw: string | null | undefined): string {
  if (!raw) return "";
  const match = raw.match(/^\s*"?([^"<]+?)"?\s*<([^>]+)>\s*$/);
  if (match) {
    const name = match[1].trim();
    return name || match[2].trim();
  }
  const trimmed = raw.trim();
  const at = trimmed.indexOf("@");
  if (at > 0) return trimmed.slice(0, at);
  return trimmed;
}

/** Memory kind → the display label used on sections and chips. */
const MEMORY_KIND_LABELS: Record<string, string> = {
  preference: "Preference",
  standing_instruction: "Standing instruction",
  fact: "Remembered fact",
  pattern: "Pattern",
};

export function humanizeMemoryKind(raw: string | null | undefined): string {
  if (!raw) return "";
  return MEMORY_KIND_LABELS[raw] ?? humanize(raw);
}

/** Section name (plural, title-case) for the four memory sections. */
const MEMORY_SECTION_LABELS: Record<string, string> = {
  preference: "Preferences",
  standing_instruction: "Standing instructions",
  fact: "Remembered facts",
  pattern: "Patterns",
};

export function memorySectionLabel(raw: string | null | undefined): string {
  if (!raw) return "";
  return MEMORY_SECTION_LABELS[raw] ?? humanize(raw);
}

/** "12:34 PM" — short clock for a log row. */
export function shortClock(
  input: string | number | null | undefined,
  now: Date = new Date(),
): string {
  if (input === null || input === undefined || input === "") return "";
  const then = typeof input === "number" ? new Date(input) : new Date(input);
  if (Number.isNaN(then.getTime())) return "";
  const sameDay =
    then.getFullYear() === now.getFullYear() &&
    then.getMonth() === now.getMonth() &&
    then.getDate() === now.getDate();
  const time = then.toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
  if (sameDay) return time;
  const date = then.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
  return `${date} · ${time}`;
}

/** RFC-2822 / ISO date → "2h ago", "Yesterday", "Apr 14". */
export function relativeTime(
  input: string | number | null | undefined,
  now: Date = new Date(),
): string {
  if (input === null || input === undefined || input === "") return "";
  const then = typeof input === "number" ? new Date(input) : new Date(input);
  if (Number.isNaN(then.getTime())) return "";
  const diffMs = now.getTime() - then.getTime();
  const mins = Math.round(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days}d ago`;
  return then.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
