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
if (!process.env.INTERNAL_SHARED_SECRET)
  throw new Error("INTERNAL_SHARED_SECRET env var is required");
const INTERNAL_SECRET: string = process.env.INTERNAL_SHARED_SECRET;

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
    .select("id, status, reference_image_id")
    .eq("id", projectId)
    .eq("workspace_id", workspaceId)
    .single();

  if (projErr || !project) return notFound("Project not found");

  // Precondition 1: reference image is set
  if (!project.reference_image_id) {
    return unprocessable("Project has no reference image set");
  }

  // Precondition 2: at least one template field exists
  const { count: fieldCount, error: fldErr } = await supabase
    .from("template_fields")
    .select("id", { count: "exact", head: true })
    .eq("project_id", projectId);
  if (fldErr) return internalError(fldErr.message);
  if ((fieldCount ?? 0) < 1) {
    return unprocessable("Project has no template fields defined");
  }

  // Precondition 3: at least 2 images in uploaded/done status
  const { data: eligibleImages, error: imgErr } = await supabase
    .from("images")
    .select("id, is_reference")
    .eq("project_id", projectId)
    .in("status", ["uploaded", "done"]);
  if (imgErr) return internalError(imgErr.message);
  if ((eligibleImages?.length ?? 0) < 2) {
    return unprocessable(
      "Project must have at least 2 uploaded images to enqueue a job"
    );
  }

  // Precondition 4: no active job already running
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

  // Non-reference images become per-image tasks
  const taskImages = (eligibleImages ?? []).filter((i) => !i.is_reference);
  const progressTotal = taskImages.length;

  // Create job
  const { data: job, error: jobErr } = await supabase
    .from("jobs")
    .insert({
      project_id: projectId,
      kind: "process_batch",
      progress_total: progressTotal,
    })
    .select(
      "id, status, kind, progress_total, progress_done, created_at"
    )
    .single();

  if (jobErr) return internalError(jobErr.message);

  // Insert image_tasks rows (one per non-reference uploaded/done image)
  if (taskImages.length > 0) {
    const taskRows = taskImages.map((img) => ({
      job_id: job.id,
      image_id: img.id,
    }));
    const { error: tasksErr } = await supabase
      .from("image_tasks")
      .insert(taskRows);
    if (tasksErr) {
      // Best-effort rollback of the job row
      await supabase.from("jobs").delete().eq("id", job.id);
      return internalError(tasksErr.message);
    }
  }

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
    headers: {
      "x-internal-token": INTERNAL_SECRET,
      "content-type": "application/json",
    },
    body: JSON.stringify({ job_id: job.id }),
  }).catch(() => {});

  return NextResponse.json({ job }, { status: 201 });
}
