"use client";

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

/**
 * Browser-side Supabase client used ONLY for Realtime subscriptions.
 *
 * It does not (and must not) read row data — the workspace cookie isn't
 * available to the anon key, so it would see other workspaces' rows. We use
 * Realtime purely as a wake-up signal: when an event fires for the subscribed
 * jobId, the page invalidates its react-query cache so the workspace-scoped
 * server route refetches authoritative data.
 *
 * Returns null if the public env vars aren't set — callers should fall back
 * to polling.
 */
let _client: SupabaseClient | null | undefined;

export function getBrowserSupabase(): SupabaseClient | null {
  if (_client !== undefined) return _client;
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !key) {
    _client = null;
    return null;
  }
  _client = createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
    realtime: { params: { eventsPerSecond: 5 } },
  });
  return _client;
}
