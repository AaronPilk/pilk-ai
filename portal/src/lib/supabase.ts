import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// Vite exposes VITE_* variables to the client bundle. The anon key is
// safe in the browser — Row Level Security policies on Supabase side do
// the real enforcement.
const url = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;

function make(): SupabaseClient | null {
  if (!url || !anonKey) return null;
  return createClient(url, anonKey, {
    auth: {
      // Keep the session in localStorage (default) and auto-refresh access
      // tokens so the portal survives tab reloads during long sessions.
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  });
}

export const supabase = make();
export const isConfigured = supabase !== null;
