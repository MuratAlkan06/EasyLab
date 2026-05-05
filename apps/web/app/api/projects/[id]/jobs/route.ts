import { NextRequest, NextResponse } from "next/server";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import {
  notFound,
  conflict,
  unprocessable,
  quotaExceeded,
  internalError,
} from "@/lib/errors";

const MAX_JOBS_PER_DAY =
  parseInt(process.env.MAX_JOBS_PER_WORKSPACE_PER_DAY ?? "5");
const FASTAPI_URL =
  process.env.FASTAPI_INTERNAL_URL ?? "http://localhost:8000";
const INTERNAL_SECRET = process.env.INTERNAL_SHARED_SECRET ?? "";

export async function POST(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: projectId } = await params;
  const workspaceId = await getWorkspaceId();
  if (!workspaceId) return notFound("No workspace");

  // Verify project belongs to workspace
  const { data: project, error: projErr } = await supabase
    .from("projects")
    .select("id, status")
    .eq("id", projectId)
    .eq("workspace_id", workspaceId)
    .single();

  if (projErr || !project) return notFound("Project not found");

  // Check no active job already running
  const { data: activeJob } = await supabase
    .from("jobs")
    .select("id")
    .eq("project_id", projectId)
    .in("status", ["queued", "running"])
    .maybeSingle();

  if (activeJob) return conflict("A job is already active for this project");

  // Quota check
  const today = new Date().toISOString().slice(0, 10);
  const { data: quota } = await supabase
    .from("workspace_quota")
    .select("jobs_today, quota_date")
    .eq("workspace_id", workspaceId)
    .maybeSingle();

  const quotaFresh = quota && quota.quota_date === today;
  const jobsToday = quotaFresh ? quota.jobs_today : 0;

  if (jobsToday >= MAX_JOBS_PER_DAY) {
    return quotaExceeded(
      `Daily job limit of ${MAX_JOBS_PER_DAY} reached. Try again tomorrow.`
    );
  }

  // Fake jobs use 5 hardcoded steps (no images required in Phase 1)
  const FAKE_STEPS = 5;

  // Create job
  const { data: job, error: jobErr } = await supabase
    .from("jobs")
    .insert({
      project_id: projectId,
      kind: "fake",
      progress_total: FAKE_STEPS,
    })
    .select("id, status, kind, progress_total, progress_done, created_at")
    .single();

  if (jobErr) return internalError(jobErr.message);

  // Upsert quota counter
  if (quotaFresh) {
    await supabase
      .from("workspace_quota")
      .update({ jobs_today: jobsToday + 1 })
      .eq("workspace_id", workspaceId);
  } else {
    await supabase.from("workspace_quota").upsert({
      workspace_id: workspaceId,
      jobs_today: 1,
      quota_date: today,
    });
  }

  // Kick the worker (best-effort — job will be picked up on next poll anyway)
  fetch(`${FASTAPI_URL}/internal/kick`, {
    method: "POST",
    headers: { "x-internal-token": INTERNAL_SECRET },
  }).catch(() => {});

  return NextResponse.json({ job }, { status: 201 });
}
