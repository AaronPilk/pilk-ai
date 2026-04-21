import { useEffect, useState } from "react";
import { apiFetch, wsUrlWithAuth } from "../lib/api";

export type WsStatus = "connecting" | "open" | "closed";

type Listener = (msg: any) => void;

class PilkSocket {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private statusListeners = new Set<(s: WsStatus) => void>();
  private status: WsStatus = "closed";
  private reconnectTimer: number | null = null;

  async connect() {
    if (this.ws) return;
    this.setStatus("connecting");
    const url = await wsUrlWithAuth();
    // Another connect() may have raced in during the await — bail if so.
    if (this.ws) return;
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => this.setStatus("open");
    ws.onclose = () => {
      this.ws = null;
      this.setStatus("closed");
      this.scheduleReconnect();
    };
    ws.onerror = () => {
      ws.close();
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        for (const l of this.listeners) l(msg);
      } catch {
        // ignore non-json frames
      }
    };
  }

  private scheduleReconnect() {
    if (this.reconnectTimer) return;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, 1500);
  }

  private setStatus(s: WsStatus) {
    this.status = s;
    for (const l of this.statusListeners) l(s);
  }

  send(msg: unknown) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  onMessage(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  }

  onStatus(fn: (s: WsStatus) => void): () => void {
    this.statusListeners.add(fn);
    fn(this.status);
    return () => {
      this.statusListeners.delete(fn);
    };
  }
}

export const pilk = new PilkSocket();
void pilk.connect();

export function useConnection() {
  const [status, setStatus] = useState<WsStatus>("closed");
  useEffect(() => pilk.onStatus(setStatus), []);
  return { status };
}

// ── REST helpers ──────────────────────────────────────────────────────

export async function fetchPlans(): Promise<{
  plans: PlanSummary[];
  running_plan_id: string | null;
}> {
  const r = await apiFetch(`/plans`);
  if (!r.ok) throw new Error(`GET /plans failed: ${r.status}`);
  return r.json();
}

export async function fetchPlan(id: string): Promise<PlanDetail> {
  const r = await apiFetch(`/plans/${id}`);
  if (!r.ok) throw new Error(`GET /plans/${id} failed: ${r.status}`);
  return r.json();
}

export async function cancelPlan(id: string, reason?: string): Promise<{
  cancelled: boolean;
  plan_id: string;
  reason: string;
  closed_browser_sessions: string[];
}> {
  const r = await apiFetch(`/plans/${encodeURIComponent(id)}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: reason ?? null }),
  });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function cancelAllRunning(): Promise<{
  cancelled_plan_id: string | null;
  closed_browser_sessions: string[];
}> {
  const r = await apiFetch(`/plans/cancel-all`, { method: "POST" });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function fetchCostSummary(): Promise<CostSummary> {
  const r = await apiFetch(`/cost/summary`);
  if (!r.ok) throw new Error(`GET /cost/summary failed: ${r.status}`);
  return r.json();
}

export async function fetchCostEntries(limit = 50): Promise<{ entries: CostEntry[] }> {
  const r = await apiFetch(`/cost/entries?limit=${limit}`);
  if (!r.ok) throw new Error(`GET /cost/entries failed: ${r.status}`);
  return r.json();
}

export type AutonomyProfile =
  | "observer"
  | "assistant"
  | "operator"
  | "autonomous";

export const AUTONOMY_PROFILES: AutonomyProfile[] = [
  "observer",
  "assistant",
  "operator",
  "autonomous",
];

export async function fetchAgents(): Promise<{
  agents: AgentRow[];
  profiles?: AutonomyProfile[];
}> {
  const r = await apiFetch(`/agents`);
  if (!r.ok) throw new Error(`GET /agents failed: ${r.status}`);
  return r.json();
}

export async function setAgentPolicy(
  name: string,
  profile: AutonomyProfile,
): Promise<{ agent: string; profile: AutonomyProfile }> {
  const r = await apiFetch(
    `/agents/${encodeURIComponent(name)}/policy`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile }),
    },
  );
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function runAgent(name: string, task: string): Promise<void> {
  const r = await apiFetch(`/agents/${name}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task }),
  });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const body = await r.json();
      if (body?.detail) detail = body.detail;
    } catch {}
    throw new Error(detail);
  }
}

export async function fetchSandboxes(): Promise<{ sandboxes: SandboxRow[] }> {
  const r = await apiFetch(`/sandboxes`);
  if (!r.ok) throw new Error(`GET /sandboxes failed: ${r.status}`);
  return r.json();
}

export interface BrowserSession {
  id: string;
  live_view_url: string;
  agent_name: string | null;
  sandbox_id: string | null;
  plan_id: string | null;
  status: "open" | "closed" | "errored";
  current_url: string | null;
  page_title: string | null;
  created_at: number;
  last_action: string | null;
  last_action_at: number;
}

export interface BrowserAction {
  session_id: string;
  plan_id: string | null;
  agent_name: string | null;
  action: string;
  detail: Record<string, unknown>;
  at: number;
}

export async function fetchBrowserSessions(): Promise<{
  enabled: boolean;
  sessions: BrowserSession[];
  active: BrowserSession[];
}> {
  const r = await apiFetch(`/browser/sessions`);
  if (!r.ok) throw new Error(`GET /browser/sessions failed: ${r.status}`);
  return r.json();
}

export async function fetchApprovals(): Promise<{
  pending: ApprovalRequest[];
  recent: ApprovalHistoryRow[];
}> {
  const r = await apiFetch(`/approvals`);
  if (!r.ok) throw new Error(`GET /approvals failed: ${r.status}`);
  return r.json();
}

export async function approveApproval(
  id: string,
  body: { reason?: string; trust?: { scope: TrustScope; ttl_seconds: number } },
): Promise<void> {
  const r = await apiFetch(`/approvals/${id}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await detail(r));
}

export async function rejectApproval(
  id: string,
  body: { reason?: string },
): Promise<void> {
  const r = await apiFetch(`/approvals/${id}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await detail(r));
}

export async function approveAllPending(reason?: string): Promise<{
  approved: string[];
  count: number;
}> {
  const r = await apiFetch(`/approvals/batch/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: reason ?? null }),
  });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function fetchTrust(): Promise<{ rules: TrustRule[] }> {
  const r = await apiFetch(`/trust`);
  if (!r.ok) throw new Error(`GET /trust failed: ${r.status}`);
  return r.json();
}

export async function revokeTrust(id: string): Promise<void> {
  const r = await apiFetch(`/trust/${id}`, { method: "DELETE" });
  if (!r.ok) throw new Error(await detail(r));
}

export async function fetchVoiceStatus(): Promise<VoiceStatus> {
  const r = await apiFetch(`/voice/status`);
  if (!r.ok) throw new Error(`GET /voice/status failed: ${r.status}`);
  return r.json();
}

export async function voiceListen(): Promise<void> {
  const r = await apiFetch(`/voice/listen`, { method: "POST" });
  if (!r.ok) throw new Error(await detail(r));
}

export async function voiceCancel(): Promise<void> {
  const r = await apiFetch(`/voice/cancel`, { method: "POST" });
  if (!r.ok) throw new Error(await detail(r));
}

export async function voiceDone(): Promise<void> {
  const r = await apiFetch(`/voice/done`, { method: "POST" });
  if (!r.ok) throw new Error(await detail(r));
}

export async function voiceUtterance(
  blob: Blob,
): Promise<VoiceUtteranceResult> {
  const form = new FormData();
  form.append("audio", blob, "utterance.webm");
  const r = await apiFetch(`/voice/utterance`, {
    method: "POST",
    body: form,
  });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export interface VoiceSpeakResult {
  audio_b64: string;
  audio_mime: string;
  tts_provider: string;
  usd: number;
}

// ── Governor ──────────────────────────────────────────────────────

export type TierKey = "light" | "standard" | "premium";
export type OverrideMode = "auto" | TierKey;
export type PremiumGate = "ask" | "auto";

export interface TierSpecPublic {
  tier: TierKey;
  label: string;
  provider: string;
  model: string;
}

export interface GovernorBudget {
  spent_usd: number;
  cap_usd: number;
  warn_at_usd: number;
  is_over: boolean;
  is_warn: boolean;
}

export interface GovernorStatus {
  enabled: boolean;
  tiers?: { light: TierSpecPublic; standard: TierSpecPublic; premium: TierSpecPublic };
  override?: OverrideMode;
  premium_gate?: PremiumGate;
  budget?: GovernorBudget;
}

export async function fetchGovernorStatus(): Promise<GovernorStatus> {
  const r = await apiFetch(`/governor/status`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function setGovernorOverride(mode: OverrideMode): Promise<void> {
  const r = await apiFetch(`/governor/override`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  if (!r.ok) throw new Error(await detail(r));
}

export async function setGovernorConfig(body: {
  daily_cap_usd?: number;
  premium_gate?: PremiumGate;
}): Promise<GovernorStatus> {
  const r = await apiFetch(`/governor/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await detail(r));
  const s = await r.json();
  return { enabled: true, ...s };
}

// ── Connected accounts (OAuth-first) ─────────────────────────────

export interface ProviderScope {
  name: string;
  label: string;
  risk: string;
}

export interface ProviderScopeGroup {
  name: string;
  label: string;
}

export interface ProviderInfo {
  name: string;
  label: string;
  supports_roles: Array<"system" | "user">;
  scopes: ProviderScope[];
  scope_groups: ProviderScopeGroup[];
  default_scope_groups: string[];
  configured: boolean;
  setup_hint: string | null;
}

export interface ConnectedAccount {
  account_id: string;
  provider: string;
  role: "system" | "user";
  label: string;
  email: string | null;
  username: string | null;
  scopes: string[];
  status: "connected" | "expired" | "revoked" | "pending";
  linked_at: string;
  last_refreshed_at: string | null;
}

export async function fetchProviders(): Promise<{ providers: ProviderInfo[] }> {
  const r = await apiFetch(`/integrations/providers`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function fetchConnectedAccounts(): Promise<{
  accounts: ConnectedAccount[];
  defaults: Record<string, string>;
}> {
  const r = await apiFetch(`/integrations/accounts`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function startOAuthConnection(body: {
  provider: string;
  role: "system" | "user";
  make_default?: boolean;
  scope_groups?: string[];
}): Promise<{
  auth_url: string;
  state: string;
  redirect_uri: string;
  scope_groups?: string[];
}> {
  const r = await apiFetch(`/integrations/accounts/oauth/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function deleteConnectedAccount(accountId: string): Promise<void> {
  const r = await apiFetch(
    `/integrations/accounts/${encodeURIComponent(accountId)}`,
    { method: "DELETE" },
  );
  if (!r.ok) throw new Error(await detail(r));
}

export async function setDefaultConnectedAccount(
  accountId: string,
): Promise<void> {
  const r = await apiFetch(
    `/integrations/accounts/${encodeURIComponent(accountId)}/default`,
    { method: "POST" },
  );
  if (!r.ok) throw new Error(await detail(r));
}

export interface AgentGrant {
  agent_name: string;
  accounts: string[];
  granted_at: string | null;
  granted_by: string;
}

export async function fetchGrants(): Promise<{
  grants: Record<string, AgentGrant>;
}> {
  const r = await apiFetch(`/integrations/grants`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function grantAgentAccess(
  accountId: string,
  agentName: string,
): Promise<void> {
  const r = await apiFetch(
    `/integrations/accounts/${encodeURIComponent(
      accountId,
    )}/agents/${encodeURIComponent(agentName)}`,
    { method: "POST" },
  );
  if (!r.ok) throw new Error(await detail(r));
}

export async function revokeAgentAccess(
  accountId: string,
  agentName: string,
): Promise<void> {
  const r = await apiFetch(
    `/integrations/accounts/${encodeURIComponent(
      accountId,
    )}/agents/${encodeURIComponent(agentName)}`,
    { method: "DELETE" },
  );
  if (!r.ok) throw new Error(await detail(r));
}

// ── Integrations ─────────────────────────────────────────────────

export type GoogleRole = "system" | "user";

export interface GoogleIntegrationStatus {
  linked: boolean;
  email: string | null;
  scopes: string[];
  linked_at: string | null;
  error: string | null;
  role: GoogleRole;
  label: string;
}

export interface IntegrationsStatus {
  google: Record<GoogleRole, GoogleIntegrationStatus>;
}

export async function fetchIntegrationsStatus(): Promise<IntegrationsStatus> {
  const r = await apiFetch(`/integrations/status`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export interface InboxGlancePreview {
  from: string;
  subject: string;
  received_at: string;
}

export interface InboxGlance {
  linked: boolean;
  email: string | null;
  unread: number;
  preview: InboxGlancePreview[];
  role: GoogleRole;
  error?: string;
}

export async function fetchInboxGlance(
  role: GoogleRole = "user",
): Promise<InboxGlance> {
  const r = await apiFetch(
    `/integrations/google/${encodeURIComponent(role)}/inbox/glance`,
  );
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export interface CalendarGlancePreview {
  summary: string;
  start: string;
  end: string;
}

export interface CalendarGlance {
  linked: boolean;
  email: string | null;
  events_count: number;
  preview: CalendarGlancePreview[];
  role: GoogleRole;
  scope_missing?: boolean;
  error?: string;
}

export async function fetchCalendarGlance(
  role: GoogleRole = "user",
): Promise<CalendarGlance> {
  const r = await apiFetch(
    `/integrations/google/${encodeURIComponent(role)}/calendar/glance`,
  );
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

// ── Apple Messages (local macOS reader) ─────────────────────────

export interface MessagesThreadPreview {
  chat_id: number;
  title: string;
  is_group: boolean;
  last_at: string;
  last_snippet: string;
  last_from_me: boolean;
}

export interface MessagesGlance {
  available: boolean;
  threads: MessagesThreadPreview[];
  reason?: string;
  db_path?: string;
  error?: string;
}

export async function fetchMessagesGlance(): Promise<MessagesGlance> {
  const r = await apiFetch(`/integrations/apple/messages/glance`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function voiceSpeak(text: string): Promise<VoiceSpeakResult> {
  const r = await apiFetch(`/voice/speak`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

// ── Memory ─────────────────────────────────────────────────────

export type MemoryKind =
  | "preference"
  | "standing_instruction"
  | "fact"
  | "pattern";

export interface MemoryEntry {
  id: string;
  kind: MemoryKind;
  title: string;
  body: string;
  source: string;
  plan_id: string | null;
  created_at: string;
  updated_at: string;
}

export async function fetchMemory(kind?: MemoryKind): Promise<{
  entries: MemoryEntry[];
  kinds: MemoryKind[];
}> {
  const q = kind ? `?kind=${encodeURIComponent(kind)}` : "";
  const r = await apiFetch(`/memory${q}`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function addMemory(body: {
  kind: MemoryKind;
  title: string;
  body: string;
}): Promise<MemoryEntry> {
  const r = await apiFetch(`/memory`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function deleteMemory(id: string): Promise<void> {
  const r = await apiFetch(`/memory/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error(await detail(r));
}

export async function clearMemory(kind?: MemoryKind): Promise<{ cleared: number }> {
  const q = kind ? `?kind=${encodeURIComponent(kind)}` : "";
  const r = await apiFetch(`/memory${q}`, { method: "DELETE" });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export interface MemoryProposal {
  kind: MemoryKind;
  title: string;
  body: string;
  confidence: number;
  rationale: string;
}

export async function distillMemory(
  window = 30,
): Promise<{ proposals: MemoryProposal[]; window: number }> {
  const r = await apiFetch(`/memory/distill`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ window }),
  });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

// ── Sentinel ─────────────────────────────────────────────────

export type SentinelSeverity = "low" | "med" | "high" | "critical";

export interface SentinelIncident {
  id: string;
  agent: string | null;
  severity: SentinelSeverity;
  category: string;
  kind: string;
  summary: string;
  likely_cause: string | null;
  recommended_action: string | null;
  remediation: string | null;
  outcome: string | null;
  acknowledged_at: string | null;
  created_at: string;
}

export interface SentinelSummary {
  unacked_count: number;
  top_unacked: SentinelIncident[];
}

export async function fetchSentinelSummary(): Promise<SentinelSummary> {
  const r = await apiFetch(`/sentinel/summary`);
  if (!r.ok) throw new Error(`GET /sentinel/summary failed: ${r.status}`);
  return r.json();
}

export async function fetchSentinelIncidents(opts?: {
  limit?: number;
  only_unacked?: boolean;
  min_severity?: SentinelSeverity;
}): Promise<{ incidents: SentinelIncident[] }> {
  const params = new URLSearchParams();
  if (opts?.limit) params.set("limit", String(opts.limit));
  if (opts?.only_unacked) params.set("only_unacked", "true");
  if (opts?.min_severity) params.set("min_severity", opts.min_severity);
  const qs = params.toString();
  const r = await apiFetch(`/sentinel/incidents${qs ? `?${qs}` : ""}`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function acknowledgeSentinelIncident(
  id: string,
  reason?: string,
): Promise<{ id: string; acked: boolean }> {
  const r = await apiFetch(
    `/sentinel/incidents/${encodeURIComponent(id)}/acknowledge`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason ?? null }),
    },
  );
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

// ── Logs ───────────────────────────────────────────────────────

export type LogKind = "plan" | "approval" | "trust";

export interface LogEntryBase {
  kind: LogKind;
  id: string;
  at: string;
  title: string;
}

export interface PlanLogEntry extends LogEntryBase {
  kind: "plan";
  status: string;
  cost_usd: number;
  plan_id: string;
}

export interface ApprovalLogEntry extends LogEntryBase {
  kind: "approval";
  status: "pending" | "approved" | "rejected" | "expired";
  risk_class: string;
  reason: string;
  plan_id: string | null;
}

export interface TrustLogEntry extends LogEntryBase {
  kind: "trust";
  agent_name: string | null;
  ttl_seconds: number;
  expires_at: string;
  reason: string;
}

export type LogEntry = PlanLogEntry | ApprovalLogEntry | TrustLogEntry;

export async function fetchLogs(opts?: {
  kind?: LogKind;
  limit?: number;
  before?: string;
}): Promise<{ entries: LogEntry[]; next_before: string | null }> {
  const params = new URLSearchParams();
  if (opts?.kind) params.set("kind", opts.kind);
  if (opts?.limit) params.set("limit", String(opts.limit));
  if (opts?.before) params.set("before", opts.before);
  const qs = params.toString();
  const r = await apiFetch(`/logs${qs ? `?${qs}` : ""}`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

// ── Coding engines ───────────────────────────────────────────────

export interface CodingEngineHealth {
  name: string;
  label: string;
  available: boolean;
  detail: string;
}

export async function fetchCodingEngines(): Promise<{
  engines: CodingEngineHealth[];
}> {
  const r = await apiFetch(`/coding/engines`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export interface InstalledPack {
  name: string;
  kind: "skill" | "plugin";
  path: string;
  description: string;
}

export async function fetchInstalledSkills(): Promise<{
  skills: InstalledPack[];
  plugins: InstalledPack[];
}> {
  const r = await apiFetch(`/coding/skills`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

async function detail(r: Response): Promise<string> {
  try {
    const body = await r.json();
    if (body?.detail) return String(body.detail);
  } catch {}
  return `HTTP ${r.status}`;
}

// ── Shared types ─────────────────────────────────────────────────────

export type PlanStatus =
  | "pending"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export type StepStatus =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "skipped"
  | "awaiting_approval";

export interface PlanSummary {
  id: string;
  goal: string;
  status: PlanStatus;
  created_at: string;
  updated_at: string;
  estimated_usd: number | null;
  actual_usd: number;
}

export interface Step {
  id: string;
  plan_id: string;
  idx: number;
  kind: "llm" | "tool" | "agent" | "approval";
  description: string;
  status: StepStatus;
  risk_class: string;
  input: unknown;
  output: any;
  started_at: string | null;
  finished_at: string | null;
  cost_usd: number;
  error: string | null;
}

export interface PlanDetail extends PlanSummary {
  steps: Step[];
}

export interface CostSummary {
  day_usd: number;
  week_usd: number;
  month_usd: number;
  total_usd: number;
}

export interface CostEntry {
  id: number;
  plan_id: string | null;
  step_id: string | null;
  agent_name: string | null;
  kind: string;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  usd: number;
  occurred_at: string;
}

export interface AgentIntegration {
  name: string;
  kind: "api_key" | "oauth";
  label: string;
  role: "user" | "system" | null;
  scopes: string[];
  docs_url: string | null;
  configured: boolean;
}

export interface AgentRow {
  name: string;
  version: string;
  manifest_path: string;
  state: "registered" | "ready" | "running" | "paused" | "stopped" | "errored";
  installed_at: string;
  last_run_at: string | null;
  description?: string;
  tools?: string[];
  sandbox?: { type: string; profile: string; capabilities?: string[] };
  budget?: { per_run_usd: number; daily_usd: number };
  autonomy_profile?: AutonomyProfile;
  integrations?: AgentIntegration[];
}

export interface SandboxRow {
  id: string;
  type: string;
  agent_name: string | null;
  state: string;
  created_at: string;
  destroyed_at: string | null;
  workspace?: string;
  profile?: string;
  capabilities?: string[];
}

export type TrustScope = "none" | "agent" | "agent+args";

export interface ApprovalRequest {
  id: string;
  plan_id: string | null;
  step_id: string | null;
  agent_name: string | null;
  tool_name: string;
  args: Record<string, unknown>;
  risk_class: string;
  reason: string;
  created_at: string;
  bypass_trust: boolean;
}

export interface ApprovalHistoryRow {
  id: string;
  plan_id: string | null;
  step_id: string | null;
  agent_name: string | null;
  tool: string;
  args: Record<string, unknown>;
  risk_class: string;
  status: "pending" | "approved" | "rejected" | "expired";
  created_at: string;
  decided_at: string | null;
  decision_reason: string | null;
}

export type VoicePipelineState =
  | "idle"
  | "listening"
  | "transcribing"
  | "speaking";

export interface VoiceStatus {
  state: VoicePipelineState;
  stt_provider: string | null;
  tts_provider: string | null;
  enabled: boolean;
}

export interface VoiceUtteranceResult {
  transcript: string;
  response_text: string;
  audio_b64: string | null;
  audio_mime: string;
  stt_provider: string;
  tts_provider: string;
  usd: number;
  plan_id: string | null;
  metadata: Record<string, unknown>;
}

export interface TrustRule {
  id: string;
  agent_name: string | null;
  tool_name: string;
  args_matcher: Record<string, unknown>;
  expires_at: number;
  expires_in_s: number;
  created_at: number;
  created_by: string;
  reason: string | null;
  uses: number;
}

// ── Integration secrets (user-managed API keys) ─────────────────

export interface IntegrationSecretEntry {
  name: string;
  label: string;
  description: string;
  env: string;
  configured: boolean;
  updated_at: string | null;
}

export async function fetchIntegrationSecrets(): Promise<{
  entries: IntegrationSecretEntry[];
}> {
  const r = await apiFetch(`/integration-secrets`);
  if (!r.ok) throw new Error(`GET /integration-secrets failed: ${r.status}`);
  return r.json();
}

export async function setIntegrationSecret(
  name: string,
  value: string,
): Promise<{ name: string; configured: boolean }> {
  const r = await apiFetch(`/integration-secrets/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function clearIntegrationSecret(
  name: string,
): Promise<{ name: string; configured: boolean; removed: boolean }> {
  const r = await apiFetch(`/integration-secrets/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

// ── XAU/USD runtime settings ────────────────────────────────────

export type XAUUSDExecutionMode = "approve" | "autonomous";

export interface XAUUSDSettings {
  execution_mode: XAUUSDExecutionMode;
  is_default: boolean;
  updated_at: string | null;
  allowed_modes: XAUUSDExecutionMode[];
}

export async function fetchXAUUSDSettings(): Promise<XAUUSDSettings> {
  const r = await apiFetch(`/xauusd/settings`);
  if (!r.ok) throw new Error(`GET /xauusd/settings failed: ${r.status}`);
  return r.json();
}

export async function setXAUUSDExecutionMode(
  mode: XAUUSDExecutionMode,
): Promise<{ execution_mode: XAUUSDExecutionMode; updated_at: string | null }> {
  const r = await apiFetch(`/xauusd/settings/execution_mode`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

// ── Brain (Obsidian-compatible long-term vault) ──────────────

/** One markdown note in the vault. `folder` is empty for root-level
 * notes; nested notes carry a POSIX path like "daily/2026-04-20". */
export interface BrainNote {
  path: string;
  folder: string;
  stem: string;
  size: number;
  mtime: string | null;
}

export interface BrainSearchHit {
  path: string;
  line: number;
  snippet: string;
}

export async function fetchBrainNotes(): Promise<{
  notes: BrainNote[];
  root: string;
}> {
  const r = await apiFetch(`/brain`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function fetchBrainNote(
  path: string,
): Promise<{ path: string; body: string; size: number }> {
  const r = await apiFetch(`/brain/note?path=${encodeURIComponent(path)}`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function searchBrain(
  q: string,
): Promise<{ query: string; hits: BrainSearchHit[] }> {
  const r = await apiFetch(`/brain/search?q=${encodeURIComponent(q)}`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

// ── Telegram (PILK push channel) ───────────────────────────────

/** Shape returned by GET /telegram/bot-info. `configured` means the
 * token is set at all; `valid` means Telegram's `getMe` accepted it.
 * `t_me_url` is pre-built so the UI can link straight to the bot. */
export interface TelegramBotInfo {
  configured: boolean;
  valid?: boolean;
  bot_id?: number;
  username?: string;
  first_name?: string;
  can_join_groups?: boolean;
  t_me_url?: string | null;
  error?: string;
}

export interface TelegramDetectResult {
  detected: boolean;
  chat_id?: string;
  chat_type?: string;
  chat_title?: string;
  error?: string;
}

export interface TelegramTestResult {
  sent: boolean;
  message_id?: number;
  error?: string;
}

export async function fetchTelegramBotInfo(): Promise<TelegramBotInfo> {
  const r = await apiFetch(`/telegram/bot-info`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function detectTelegramChat(): Promise<TelegramDetectResult> {
  const r = await apiFetch(`/telegram/detect-chat`, { method: "POST" });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function sendTelegramTest(): Promise<TelegramTestResult> {
  const r = await apiFetch(`/telegram/test`, { method: "POST" });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

// ── Triggers ─────────────────────────────────────────────────────

export type TriggerScheduleKind = "cron" | "event";

export interface TriggerSchedule {
  kind: TriggerScheduleKind;
  // cron
  expression?: string;
  // event
  event_type?: string;
  filter?: Record<string, unknown>;
}

export interface TriggerRow {
  name: string;
  description: string;
  agent_name: string;
  goal: string;
  schedule: TriggerSchedule;
  enabled: boolean;
  last_fired_at: string | null;
}

export async function fetchTriggers(): Promise<{ triggers: TriggerRow[] }> {
  const r = await apiFetch(`/triggers`);
  if (!r.ok) throw new Error(`GET /triggers failed: ${r.status}`);
  return r.json();
}

export async function setTriggerEnabled(
  name: string,
  enabled: boolean,
): Promise<{ name: string; enabled: boolean }> {
  const action = enabled ? "enable" : "disable";
  const r = await apiFetch(
    `/triggers/${encodeURIComponent(name)}/${action}`,
    { method: "POST" },
  );
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function fireTrigger(
  name: string,
): Promise<{ name: string; status: string; fired_at?: string; reason?: string; error?: string }> {
  const r = await apiFetch(
    `/triggers/${encodeURIComponent(name)}/fire`,
    { method: "POST" },
  );
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}
