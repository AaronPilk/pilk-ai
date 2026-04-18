import { getAccessToken } from "./session";

// Resolve the pilkd base URL at build time. In a production Vite build
// (Cloudflare Pages → app.pilk.ai), `VITE_PILK_API` points at the
// Railway-hosted API. When running `npm run dev` on a laptop, the env
// var is empty and we fall back to the local daemon on 127.0.0.1:7424,
// preserving the pre-cloud developer loop.
const API_URL =
  (import.meta.env.VITE_PILK_API as string | undefined)?.replace(/\/$/, "") ??
  "http://127.0.0.1:7424";

const WS_URL =
  (import.meta.env.VITE_PILK_WS as string | undefined) ??
  "ws://127.0.0.1:7424/ws";

export { API_URL, WS_URL };

/** `fetch` against pilkd with auth + URL plumbing baked in.
 *
 * - `path` is joined onto `API_URL`, so callers pass route-relative
 *   paths (e.g. "/plans", "/agents/name/run").
 * - In cloud mode, a Supabase access token is attached as
 *   `Authorization: Bearer <token>` on every request. `getAccessToken()`
 *   reads from supabase-js, which auto-refreshes near expiry, so we
 *   never hand pilkd a stale JWT.
 * - In local mode, no Authorization header is sent and pilkd keeps its
 *   legacy "trust the localhost caller" behaviour.
 */
export async function apiFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = await getAccessToken();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return fetch(`${API_URL}${path}`, { ...init, headers });
}

/** Build a WebSocket URL with the current access token appended as a
 *  `?token=` query param. Browsers can't send `Authorization` headers
 *  on WS upgrades, so pilkd's WS auth looks for the token here instead
 *  (see `core/api/auth.py` comment for the matching server-side path).
 */
export async function wsUrlWithAuth(): Promise<string> {
  const token = await getAccessToken();
  if (!token) return WS_URL;
  const sep = WS_URL.includes("?") ? "&" : "?";
  return `${WS_URL}${sep}token=${encodeURIComponent(token)}`;
}
