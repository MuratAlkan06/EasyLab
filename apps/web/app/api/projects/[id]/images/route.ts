import { NextRequest, NextResponse } from "next/server";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import { internalError, notFound } from "@/lib/errors";

const BUCKET = "easylab";
const SIGNED_URL_TTL_SECONDS = 3600;

export async function GET(
  req: NextRequest,
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

  const statusFilter = req.nextUrl.searchParams.get("status");

  let query = supabase
    .from("images")
    .select(
      "id, filename, status, is_reference, size_bytes, width_px, height_px, storage_path, created_at"
    )
    .eq("project_id", projectId)
    .order("created_at", { ascending: true });

  if (statusFilter) query = query.eq("status", statusFilter);

  const { data: images, error: imgErr } = await query;
  if (imgErr) return internalError(imgErr.message);

  const result = await Promise.all(
    (images ?? []).map(async (img) => {
      let signedUrl: string | null = null;
      if (img.storage_path) {
        const { data: signed } = await supabase.storage
          .from(BUCKET)
          .createSignedUrl(img.storage_path, SIGNED_URL_TTL_SECONDS);
        signedUrl = signed?.signedUrl ?? null;
      }
      return {
        id: img.id,
        filename: img.filename,
        status: img.status,
        is_reference: img.is_reference,
        size_bytes: img.size_bytes,
        width_px: img.width_px,
        height_px: img.height_px,
        signed_url: signedUrl,
        created_at: img.created_at,
      };
    })
  );

  return NextResponse.json({ images: result });
}
