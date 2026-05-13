import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { imageSize } from "image-size";
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
  // Client-supplied hints — kept for backwards compatibility with older
  // browsers, but ignored. The server reads the actual values from the bytes
  // Supabase Storage accepted.
  size_bytes: z.number().int().min(1).optional(),
  width_px: z.number().int().min(1).optional(),
  height_px: z.number().int().min(1).optional(),
});

const ALLOWED_MIME = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/tiff",
]);
const MAX_BYTES = 52_428_800; // 50 MB — must match upload-urls validation
const MIN_DIMENSION_PX = 16; // anything smaller is unusable for annotation

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string; imageId: string }> }
) {
  const { id: projectId, imageId } = await params;
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

  // Trust Supabase Storage's mime + size, not what the client body claims.
  const stored = meta[0] as {
    metadata?: { mimetype?: string; size?: number } | null;
  };
  const storedMime = stored.metadata?.mimetype;
  const storedSize = stored.metadata?.size;

  if (!storedMime || !ALLOWED_MIME.has(storedMime)) {
    return errorResponse(
      422,
      "unprocessable",
      `Unsupported image type${storedMime ? ` '${storedMime}'` : ""}. Allowed: JPEG, PNG, WebP, TIFF.`
    );
  }

  if (typeof storedSize !== "number" || storedSize < 1) {
    return errorResponse(
      422,
      "unprocessable",
      "Uploaded file is empty or unreadable"
    );
  }

  if (storedSize > MAX_BYTES) {
    return errorResponse(
      422,
      "unprocessable",
      `File exceeds maximum size of ${Math.floor(MAX_BYTES / 1024 / 1024)} MB`
    );
  }

  // Byte-decode width + height from the actual file. The full download is
  // wasteful (we only need the header) but Supabase Storage's JS SDK doesn't
  // expose ranged reads, and a typical lab JPEG is a few MB at most. The
  // upside: width_px/height_px in the DB are now server-trusted, not whatever
  // the browser claimed.
  const { data: fileBlob, error: dlErr } = await supabase.storage
    .from("easylab")
    .download(storagePath);
  if (dlErr || !fileBlob) {
    return errorResponse(
      422,
      "unprocessable",
      "Could not read file from storage to verify dimensions"
    );
  }

  let decodedWidth: number;
  let decodedHeight: number;
  try {
    const buf = new Uint8Array(await fileBlob.arrayBuffer());
    const dims = imageSize(buf);
    if (!dims.width || !dims.height) {
      throw new Error("decoder returned no dimensions");
    }
    decodedWidth = dims.width;
    decodedHeight = dims.height;
  } catch (e) {
    return errorResponse(
      422,
      "unprocessable",
      `Could not decode image header: ${e instanceof Error ? e.message : "unknown error"}`,
    );
  }

  if (decodedWidth < MIN_DIMENSION_PX || decodedHeight < MIN_DIMENSION_PX) {
    return errorResponse(
      422,
      "unprocessable",
      `Image too small (${decodedWidth}×${decodedHeight}). Minimum ${MIN_DIMENSION_PX}px on each side.`,
    );
  }

  const updates: Record<string, unknown> = {
    status: "uploaded",
    size_bytes: storedSize,
    mime_type: storedMime,
    width_px: decodedWidth,
    height_px: decodedHeight,
  };

  const { error: updErr } = await supabase
    .from("images")
    .update(updates)
    .eq("id", imageId)
    .eq("project_id", projectId);
  if (updErr) return internalError(updErr.message);

  return NextResponse.json({
    image_id: imageId,
    status: "uploaded",
    width_px: decodedWidth,
    height_px: decodedHeight,
  });
}
