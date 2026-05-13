"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { RealtimeChannel } from "@supabase/supabase-js";
import { getBrowserSupabase } from "./supabase-browser";

/**
 * Subscribe to row changes on the active job + its image_tasks via Supabase
 * Realtime. On any event, invalidate the matching react-query key so the
 * existing workspace-scoped REST endpoint refetches fresh data.
 *
 * Returns ``true`` when a live channel is connected. The caller can use that
 * to throttle polling to a long fallback interval.
 *
 * No-op when the browser supabase client isn't configured (missing
 * NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY) — the caller's
 * polling continues unchanged in that case.
 */
export function useJobRealtime(
  projectId: string | undefined,
  jobId: string | null | undefined,
): boolean {
  const qc = useQueryClient();
  const channelRef = useRef<RealtimeChannel | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!projectId || !jobId) return;
    const sb = getBrowserSupabase();
    if (!sb) return;

    const invalidate = () => {
      qc.invalidateQueries({ queryKey: ["job", projectId, jobId] });
    };

    const channel = sb
      .channel(`job:${jobId}`)
      .on(
        "postgres_changes",
        {
          event: "*",
          schema: "public",
          table: "jobs",
          filter: `id=eq.${jobId}`,
        },
        invalidate,
      )
      .on(
        "postgres_changes",
        {
          event: "*",
          schema: "public",
          table: "image_tasks",
          filter: `job_id=eq.${jobId}`,
        },
        invalidate,
      )
      .subscribe((status) => {
        setConnected(status === "SUBSCRIBED");
      });

    channelRef.current = channel;

    return () => {
      setConnected(false);
      if (channelRef.current) {
        sb.removeChannel(channelRef.current);
        channelRef.current = null;
      }
    };
  }, [projectId, jobId, qc]);

  return connected;
}
