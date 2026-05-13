import { NextRequest, NextResponse } from "next/server";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import { conflict, internalError, notFound } from "@/lib/errors";

const BUCKET = "easylab";

export async function DELETE(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: imageId } = await params;
  const workspaceId = await getWorkspaceId();
  if (!workspaceId) return notFound("No workspace");

  // Verify image exists and belongs to a project in this workspace.
  // Disambiguate the embed: there are two FKs between images and projects
  // (images.project_id and projects.reference_image_id), so hint the column
  // name on the images side. Defaults to LEFT join — the null check below
  // catches orphans.
  const { data: image, error: imgErr } = await supabase
    .from("images")
    .select(
      "id, project_id, is_reference, storage_path, projects!project_id(workspace_id)"
    )
    .eq("id", imageId)
    .maybeSingle();
  if (imgErr) return internalError(imgErr.message);
  if (!image) return notFound("Image not found");

  const projects = image.projects as unknown as { workspace_id: string } | null;
  if (!projects || projects.workspace_id !== workspaceId) {
    return notFound("Image not found");
  }

  if (image.is_reference) {
    return conflict("Image is the reference image for its project");
  }

  // Reject if project has an active job
  const { data: activeJob, error: actJobErr } = await supabase
    .from("jobs")
    .select("id")
    .eq("project_id", image.project_id)
    .in("status", ["queued", "running"])
    .maybeSingle();
  if (actJobErr) return internalError(actJobErr.message);
  if (activeJob) return conflict("Project has a running or queued job");

  // Delete storage object (best-effort)
  if (image.storage_path) {
    await supabase.storage.from(BUCKET).remove([image.storage_path]);
  }

  // Delete DB row (cascades to cells)
  const { error: delErr } = await supabase
    .from("images")
    .delete()
    .eq("id", imageId);
  if (delErr) return internalError(delErr.message);

  return new NextResponse(null, { status: 204 });
}
