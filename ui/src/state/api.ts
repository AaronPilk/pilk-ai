import { useEffect, useState } from "react";

const API_URL =
  (import.meta.env.VITE_PILK_API as string | undefined) ?? "http://127.0.0.1:7424";
const WS_URL =
  (import.meta.env.VITE_PILK_WS as string | undefined) ?? "ws://127.0.0.1:7424/ws";

export type WsStatus = "connecting" | "open" | "closed";

type Listener = (msg: any) => void;

class PilkSocket {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private statusListeners = new Set<(s: WsStatus) => void>();
  private status: WsStatus = "closed";
  private reconnectTimer: number | null = null;

  connect() {
    if (this.ws) return;
    this.setStatus("connecting");
    const ws = new WebSocket(WS_URL);
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
      this.connect();
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
pilk.connect();

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
  const r = await fetch(`${API_URL}/plans`);
  if (!r.ok) throw new Error(`GET /plans failed: ${r.status}`);
  return r.json();
}

export async function fetchPlan(id: string): Promise<PlanDetail> {
  const r = await fetch(`${API_URL}/plans/${id}`);
  if (!r.ok) throw new Error(`GET /plans/${id} failed: ${r.status}`);
  return r.json();
}

export async function fetchCostSummary(): Promise<CostSummary> {
  const r = await fetch(`${API_URL}/cost/summary`);
  if (!r.ok) throw new Error(`GET /cost/summary failed: ${r.status}`);
  return r.json();
}

export async function fetchCostEntries(limit = 50): Promise<{ entries: CostEntry[] }> {
  const r = await fetch(`${API_URL}/cost/entries?limit=${limit}`);
  if (!r.ok) throw new Error(`GET /cost/entries failed: ${r.status}`);
  return r.json();
}

export async function fetchAgents(): Promise<{ agents: AgentRow[] }> {
  const r = await fetch(`${API_URL}/agents`);
  if (!r.ok) throw new Error(`GET /agents failed: ${r.status}`);
  return r.json();
}

export async function runAgent(name: string, task: string): Promise<void> {
  const r = await fetch(`${API_URL}/agents/${name}/run`, {
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
  const r = await fetch(`${API_URL}/sandboxes`);
  if (!r.ok) throw new Error(`GET /sandboxes failed: ${r.status}`);
  return r.json();
}

export interface BrowserSession {
  id: string;
  live_view_url: string;
  agent_name: string | null;
  sandbox_id: string | null;
  status: "open" | "closed" | "errored";
  current_url: string | null;
  page_title: string | null;
  created_at: number;
}

export async function fetchBrowserSessions(): Promise<{
  enabled: boolean;
  sessions: BrowserSession[];
  active: BrowserSession[];
}> {
  const r = await fetch(`${API_URL}/browser/sessions`);
  if (!r.ok) throw new Error(`GET /browser/sessions failed: ${r.status}`);
  return r.json();
}

export async function fetchApprovals(): Promise<{
  pending: ApprovalRequest[];
  recent: ApprovalHistoryRow[];
}> {
  const r = await fetch(`${API_URL}/approvals`);
  if (!r.ok) throw new Error(`GET /approvals failed: ${r.status}`);
  return r.json();
}

export async function approveApproval(
  id: string,
  body: { reason?: string; trust?: { scope: TrustScope; ttl_seconds: number } },
): Promise<void> {
  const r = await fetch(`${API_URL}/approvals/${id}/approve`, {
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
  const r = await fetch(`${API_URL}/approvals/${id}/reject`, {
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
  const r = await fetch(`${API_URL}/approvals/batch/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: reason ?? null }),
  });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function fetchTrust(): Promise<{ rules: TrustRule[] }> {
  const r = await fetch(`${API_URL}/trust`);
  if (!r.ok) throw new Error(`GET /trust failed: ${r.status}`);
  return r.json();
}

export async function revokeTrust(id: string): Promise<void> {
  const r = await fetch(`${API_URL}/trust/${id}`, { method: "DELETE" });
  if (!r.ok) throw new Error(await detail(r));
}

export async function fetchVoiceStatus(): Promise<VoiceStatus> {
  const r = await fetch(`${API_URL}/voice/status`);
  if (!r.ok) throw new Error(`GET /voice/status failed: ${r.status}`);
  return r.json();
}

export async function voiceListen(): Promise<void> {
  const r = await fetch(`${API_URL}/voice/listen`, { method: "POST" });
  if (!r.ok) throw new Error(await detail(r));
}

export async function voiceCancel(): Promise<void> {
  const r = await fetch(`${API_URL}/voice/cancel`, { method: "POST" });
  if (!r.ok) throw new Error(await detail(r));
}

export async function voiceDone(): Promise<void> {
  const r = await fetch(`${API_URL}/voice/done`, { method: "POST" });
  if (!r.ok) throw new Error(await detail(r));
}

export async function voiceUtterance(
  blob: Blob,
): Promise<VoiceUtteranceResult> {
  const form = new FormData();
  form.append("audio", blob, "utterance.webm");
  const r = await fetch(`${API_URL}/voice/utterance`, {
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
  const r = await fetch(`${API_URL}/governor/status`);
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}

export async function setGovernorOverride(mode: OverrideMode): Promise<void> {
  const r = await fetch(`${API_URL}/governor/override`, {
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
  const r = await fetch(`${API_URL}/governor/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await detail(r));
  const s = await r.json();
  return { enabled: true, ...s };
}

export async function voiceSpeak(text: string): Promise<VoiceSpeakResult> {
  const r = await fetch(`${API_URL}/voice/speak`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
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
