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

  onMessage(fn: Listener) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  onStatus(fn: (s: WsStatus) => void) {
    this.statusListeners.add(fn);
    fn(this.status);
    return () => this.statusListeners.delete(fn);
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
  sandbox?: { type: string; profile: string };
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
}
