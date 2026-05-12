import { NextResponse } from "next/server";
import { supabase } from "@/lib/supabase";
import { getOrCreateWorkspace } from "@/lib/workspace";
import { internalError } from "@/lib/errors";

const MAX_JOBS_PER_DAY = parseInt(
  process.env.MAX_JOBS_PER_WORKSPACE_PER_DAY ?? "5",
);
const MAX_IMAGES_PER_DAY = parseInt(
  process.env.MAX_IMAGES_PER_WORKSPACE_PER_DAY ?? "200",
);
// Mirrors WORKSPACE_DAILY_TOKEN_BUDGET in apps/worker/app/settings.py.
// Both services need the same number; if you raise one, raise the other.
const TOKENS_PER_DAY = parseInt(
  process.env.WORKSPACE_DAILY_TOKEN_BUDGET ?? "2000000",
);

export async function GET() {
  const workspaceId = await getOrCreateWorkspace();
  const today = new Date().toISOString().slice(0, 10);

  const { data, error } = await supabase
    .from("workspace_quota")
    .select("jobs_today, images_today, tokens_today, quota_date")
    .eq("workspace_id", workspaceId)
    .maybeSingle();

  if (error) return internalError(error.message);

  // Stale row from a prior day → treat counters as zero. The workspace_quota
  // row gets rewritten lazily on the next write (see jobs/route.ts and
  // record_token_usage in the worker).
  const fresh = data && data.quota_date === today;
  const jobs_today = fresh ? data.jobs_today ?? 0 : 0;
  const images_today = fresh ? data.images_today ?? 0 : 0;
  const tokens_today = fresh ? Number(data.tokens_today ?? 0) : 0;

  return NextResponse.json({
    quota_date: today,
    jobs: {
      used: jobs_today,
      limit: MAX_JOBS_PER_DAY,
      remaining: Math.max(0, MAX_JOBS_PER_DAY - jobs_today),
    },
    images: {
      used: images_today,
      limit: MAX_IMAGES_PER_DAY,
      remaining: Math.max(0, MAX_IMAGES_PER_DAY - images_today),
    },
    tokens: {
      used: tokens_today,
      limit: TOKENS_PER_DAY,
      remaining: Math.max(0, TOKENS_PER_DAY - tokens_today),
    },
  });
}
