import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import {
  conflict,
  internalError,
  notFound,
  validationError,
} from "@/lib/errors";

const BUCKET = "easylab";
const ALLOWED_MIME = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/tiff",
] as const;
const MAX_BYTES = 52_428_800; // 50 MB

const FileEntrySchema = z.object({
  filename: z.string().min(1).max(255),
  mime_type: z.enum(ALLOWED_MIME),
  size_bytes: z.number().int().min(1).max(MAX_BYTES),
});

const RequestSchema = z.object({
  files: z.array(FileEntrySchema).min(1).max(200),
});

export async function POST(
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

  // Reject if active job
  const { data: activeJob, error: actJobErr } = await supabase
    .from("jobs")
    .select("id")
    .eq("project_id", projectId)
    .in("status", ["queued", "running"])
    .maybeSingle();
  if (actJobErr) return internalError(actJobErr.message);
  if (activeJob) return conflict("Project has a running or queued job");

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

  const uploads: Array<{
    image_id: string;
    storage_path: string;
    upload_url: string;
    token: string;
    filename: string;
  }> = [];

  for (const f of parsed.data.files) {
    // Insert image row first to get its id, with a placeholder storage_path
    // (cannot satisfy NOT NULL with empty string; use temporary value, then update).
    const tempPath = `projects/${projectId}/originals/__pending__`;
    const { data: img, error: insErr } = await supabase
      .from("images")
      .insert({
        project_id: projectId,
        filename: f.filename,
        mime_type: f.mime_type,
        size_bytes: f.size_bytes,
        storage_path: tempPath,
        status: "pending_upload",
      })
      .select("id")
      .single();

    if (insErr || !img) {
      return internalError(insErr?.message ?? "Failed to insert image row");
    }

    const storagePath = `projects/${projectId}/originals/${img.id}`;

    const { error: updErr } = await supabase
      .from("images")
      .update({ storage_path: storagePath })
      .eq("id", img.id);
    if (updErr) return internalError(updErr.message);

    const { data: signed, error: sigErr } = await supabase.storage
      .from(BUCKET)
      .createSignedUploadUrl(storagePath);

    if (sigErr || !signed) {
      return internalError(
        sigErr?.message ?? "Failed to mint signed upload URL"
      );
    }

    uploads.push({
      image_id: img.id,
      storage_path: storagePath,
      upload_url: signed.signedUrl,
      token: signed.token,
      filename: f.filename,
    });
  }

  return NextResponse.json({ uploads }, { status: 201 });
}
