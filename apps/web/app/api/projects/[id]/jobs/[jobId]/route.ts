import { NextRequest, NextResponse } from "next/server";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import { notFound, internalError } from "@/lib/errors";

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string; jobId: string }> }
) {
  const { id: projectId, jobId } = await params;
  const workspaceId = await getWorkspaceId();
  if (!workspaceId) return notFound("No workspace");

  // Verify project belongs to workspace
  const { data: project } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .eq("workspace_id", workspaceId)
    .single();

  if (!project) return notFound("Project not found");

  const { data: job, error: jobErr } = await supabase
    .from("jobs")
    .select(
      "id, status, kind, progress_total, progress_done, attempts, error, created_at, started_at, finished_at"
    )
    .eq("id", jobId)
    .eq("project_id", projectId)
    .single();

  if (jobErr || !job) return notFound("Job not found");

  const { data: tasks, error: tasksErr } = await supabase
    .from("image_tasks")
    .select("image_id, status, error, updated_at, images(filename)")
    .eq("job_id", jobId);

  if (tasksErr) return internalError(tasksErr.message);

  return NextResponse.json({
    job,
    tasks: (tasks ?? []).map((t) => ({
      image_id: t.image_id,
      filename: (t.images as unknown as { filename: string } | null)?.filename ?? null,
      status: t.status,
      error: t.error,
      updated_at: t.updated_at,
    })),
  });
}
