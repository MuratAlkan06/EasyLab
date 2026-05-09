import { NextRequest, NextResponse } from "next/server";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import { conflict, internalError, notFound } from "@/lib/errors";

const BUCKET = "easylab";

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: projectId } = await params;
  const workspaceId = await getWorkspaceId();
  if (!workspaceId) return notFound("No workspace");

  const { data: project, error: projErr } = await supabase
    .from("projects")
    .select(
      "id, name, status, reference_image_id, created_at, updated_at"
    )
    .eq("id", projectId)
    .eq("workspace_id", workspaceId)
    .maybeSingle();

  if (projErr) return internalError(projErr.message);
  if (!project) return notFound("Project not found");

  // image count (excluding pending_upload)
  const { count: imageCount, error: imgErr } = await supabase
    .from("images")
    .select("id", { count: "exact", head: true })
    .eq("project_id", projectId)
    .neq("status", "pending_upload");
  if (imgErr) return internalError(imgErr.message);

  // field count
  const { count: fieldCount, error: fldErr } = await supabase
    .from("template_fields")
    .select("id", { count: "exact", head: true })
    .eq("project_id", projectId);
  if (fldErr) return internalError(fldErr.message);

  // active job presence
  const { data: activeJob, error: actJobErr } = await supabase
    .from("jobs")
    .select("id")
    .eq("project_id", projectId)
    .in("status", ["queued", "running"])
    .maybeSingle();
  if (actJobErr) return internalError(actJobErr.message);

  // latest job (any status)
  const { data: latestJob, error: latestErr } = await supabase
    .from("jobs")
    .select("id")
    .eq("project_id", projectId)
    .order("created_at", { ascending: false })
    .limit(1)
    .maybeSingle();
  if (latestErr) return internalError(latestErr.message);

  return NextResponse.json({
    id: project.id,
    name: project.name,
    status: project.status,
    reference_image_id: project.reference_image_id,
    image_count: imageCount ?? 0,
    field_count: fieldCount ?? 0,
    has_active_job: !!activeJob,
    latest_job_id: latestJob?.id ?? null,
    created_at: project.created_at,
    updated_at: project.updated_at,
  });
}

export async function DELETE(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: projectId } = await params;
  const workspaceId = await getWorkspaceId();
  if (!workspaceId) return notFound("No workspace");

  const { data: project, error: projErr } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .eq("workspace_id", workspaceId)
    .maybeSingle();

  if (projErr) return internalError(projErr.message);
  if (!project) return notFound("Project not found");

  // Reject if active job
  const { data: activeJob, error: actJobErr } = await supabase
    .from("jobs")
    .select("id")
    .eq("project_id", projectId)
    .in("status", ["queued", "running"])
    .maybeSingle();
  if (actJobErr) return internalError(actJobErr.message);
  if (activeJob)
    return conflict("Project has a running job; wait or cancel first");

  // Delete storage objects under projects/{id}/{originals,thumbs,crops}
  const prefix = `projects/${projectId}`;
  for (const sub of ["originals", "thumbs", "crops"]) {
    const folder = `${prefix}/${sub}`;
    const { data: files } = await supabase.storage
      .from(BUCKET)
      .list(folder, { limit: 1000 });
    if (files && files.length > 0) {
      const paths = files.map((f) => `${folder}/${f.name}`);
      await supabase.storage.from(BUCKET).remove(paths);
    }
  }

  // Delete DB row (cascades)
  const { error: delErr } = await supabase
    .from("projects")
    .delete()
    .eq("id", projectId)
    .eq("workspace_id", workspaceId);
  if (delErr) return internalError(delErr.message);

  return new NextResponse(null, { status: 204 });
}
