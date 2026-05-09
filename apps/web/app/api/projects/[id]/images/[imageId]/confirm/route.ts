import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import {
  conflict,
  errorResponse,
  internalError,
  notFound,
  validationError,
} from "@/lib/errors";

const ConfirmSchema = z.object({
  size_bytes: z.number().int().min(1).optional(),
  width_px: z.number().int().min(1).optional(),
  height_px: z.number().int().min(1).optional(),
});

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string; imageId: string }> }
) {
  const { id: projectId, imageId } = await params;
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

  // Optional body
  let body: unknown = {};
  const text = await req.text();
  if (text.length > 0) {
    try {
      body = JSON.parse(text);
    } catch {
      return validationError("Invalid JSON body");
    }
  }
  const parsed = ConfirmSchema.safeParse(body);
  if (!parsed.success) {
    return validationError(parsed.error.issues[0].message);
  }

  // Fetch image and verify it is in pending_upload
  const { data: image, error: imgErr } = await supabase
    .from("images")
    .select("id, status, storage_path")
    .eq("id", imageId)
    .eq("project_id", projectId)
    .maybeSingle();
  if (imgErr) return internalError(imgErr.message);
  if (!image) return notFound("Image not found");
  if (image.status !== "pending_upload") {
    return conflict(`Image is in status '${image.status}', not 'pending_upload'`);
  }

  // Verify the object was actually uploaded
  const storagePath = image.storage_path as string;
  const { data: meta, error: metaErr } = await supabase.storage
    .from("easylab")
    .list(storagePath.substring(0, storagePath.lastIndexOf("/")), {
      search: storagePath.substring(storagePath.lastIndexOf("/") + 1),
    });

  if (metaErr || !meta || meta.length === 0) {
    return errorResponse(
      422,
      "unprocessable",
      "File not found in storage — upload may have failed"
    );
  }

  const updates: Record<string, unknown> = { status: "uploaded" };
  if (parsed.data.size_bytes !== undefined)
    updates.size_bytes = parsed.data.size_bytes;
  if (parsed.data.width_px !== undefined)
    updates.width_px = parsed.data.width_px;
  if (parsed.data.height_px !== undefined)
    updates.height_px = parsed.data.height_px;

  const { error: updErr } = await supabase
    .from("images")
    .update(updates)
    .eq("id", imageId)
    .eq("project_id", projectId);
  if (updErr) return internalError(updErr.message);

  return NextResponse.json({ image_id: imageId, status: "uploaded" });
}
