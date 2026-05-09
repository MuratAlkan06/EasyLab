import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import {
  internalError,
  notFound,
  unprocessable,
  validationError,
} from "@/lib/errors";

const RequestSchema = z.object({
  image_id: z.string().uuid(),
});

export async function PUT(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: projectId } = await params;
  const workspaceId = await getWorkspaceId();
  if (!workspaceId) return notFound("No workspace");

  // Verify project belongs to workspace
  const { data: project, error: projErr } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .eq("workspace_id", workspaceId)
    .maybeSingle();
  if (projErr) return internalError(projErr.message);
  if (!project) return notFound("Project not found");

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return validationError("Invalid JSON body");
  }
  const parsed = RequestSchema.safeParse(body);
  if (!parsed.success) {
    return validationError(parsed.error.issues[0].message);
  }
  const { image_id: imageId } = parsed.data;

  // Verify image belongs to this project
  const { data: image, error: imgErr } = await supabase
    .from("images")
    .select("id, status")
    .eq("id", imageId)
    .eq("project_id", projectId)
    .maybeSingle();
  if (imgErr) return internalError(imgErr.message);
  if (!image) return notFound("Image not found");

  if (image.status !== "uploaded" && image.status !== "done") {
    return unprocessable(
      `Image must be in 'uploaded' or 'done' status (got '${image.status}')`
    );
  }

  // Clear any existing reference image for this project
  const { error: clearErr } = await supabase
    .from("images")
    .update({ is_reference: false })
    .eq("project_id", projectId)
    .eq("is_reference", true);
  if (clearErr) return internalError(clearErr.message);

  // Set new reference flag
  const { error: setErr } = await supabase
    .from("images")
    .update({ is_reference: true })
    .eq("id", imageId);
  if (setErr) return internalError(setErr.message);

  // Update project pointer
  const { error: projUpdErr } = await supabase
    .from("projects")
    .update({ reference_image_id: imageId })
    .eq("id", projectId);
  if (projUpdErr) return internalError(projUpdErr.message);

  return NextResponse.json({
    project_id: projectId,
    reference_image_id: imageId,
  });
}
