import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// The Vite dashboard runs in two modes:
//   1. Cloud (app.pilk.ai) — VITE_SUPABASE_URL + VITE_SUPABASE_ANON_KEY
//      are baked into the production build. A Supabase client is created
//      and session tokens flow to pilkd on Railway as bearer headers.
//   2. Local (127.0.0.1:1420) — no Supabase env vars; this module
//      exports `null` and the UI falls back to trust-the-caller semantics
//      (pilkd on the same machine is already localhost-trusted).
//
// The anon key is safe in the browser — Row Level Security on Supabase
// is what actually enforces per-user isolation.
const url = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;

function make(): SupabaseClient | null {
  if (!url || !anonKey) return null;
  return createClient(url, anonKey, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  });
}

export const supabase = make();
export const isCloudMode = supabase !== null;
