import { useEffect, useState } from "react";

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

export function useMessages(filter?: (m: any) => boolean) {
  const [messages, setMessages] = useState<any[]>([]);
  useEffect(
    () =>
      pilk.onMessage((m) => {
        if (!filter || filter(m)) setMessages((prev) => [...prev, m]);
      }),
    [filter]
  );
  return messages;
}
