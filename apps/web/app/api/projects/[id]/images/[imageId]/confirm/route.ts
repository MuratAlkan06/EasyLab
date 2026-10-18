import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { disableTypes, imageSize, types as imageSizeTypes } from "image-size";
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

// Accepted stored mime -> the image-size type its bytes must decode as. JPEG
// and PNG only, matching upload-urls validation.
const IMAGE_TYPE_BY_MIME = new Map<string, string>([
  ["image/jpeg", "jpg"],
  ["image/png", "png"],
]);
// Every other image-size parser is disabled, so a crafted file of another
// format is rejected at detection and never reaches that format's parser
// (CVE-2025-71329/71330: infinite loops in the ICNS/JXL/HEIF parsers).
// Derived from the library's own list so newly added parsers are off too.
const ENABLED_IMAGE_TYPES = new Set(IMAGE_TYPE_BY_MIME.values());
const DISABLED_IMAGE_TYPES = imageSizeTypes.filter(
  (t) => !ENABLED_IMAGE_TYPES.has(t),
);
const MAX_BYTES = 20_971_520; // 20 MB — matches upload-urls validation
const MIN_DIMENSION_PX = 16; // anything smaller is unusable for annotation
// EXIF orientations whose display transform rotates the stored pixels by 90°
// (transpose, rotate 90° CW, transverse, rotate 90° CCW), swapping the axes.
const AXIS_SWAPPING_ORIENTATIONS = new Set([5, 6, 7, 8]);

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

  const expectedType = storedMime ? IMAGE_TYPE_BY_MIME.get(storedMime) : undefined;
  if (!expectedType) {
    return errorResponse(
      422,
      "unprocessable",
      `Unsupported image type${storedMime ? ` '${storedMime}'` : ""}. Allowed: JPEG, PNG.`
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
  //
  // width_px/height_px are UPRIGHT dimensions (EXIF orientation applied):
  // the space the browser displays, the user annotates in, and the worker
  // normalizes boxes and crops in. image-size reports the RAW stored
  // width/height plus a separate EXIF `orientation` it does not apply, so the
  // axes are swapped here for orientations 5–8 (e.g. portrait phone photos).
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

  let decodedType: string | undefined;
  let decodedWidth: number;
  let decodedHeight: number;
  try {
    const buf = new Uint8Array(await fileBlob.arrayBuffer());
    // Re-applied before every decode: the disabled set is process-global and
    // any other importer of image-size could replace it. imageSize is
    // synchronous, so nothing can interleave between the two calls.
    disableTypes(DISABLED_IMAGE_TYPES);
    const dims = imageSize(buf);
    decodedType = dims.type;
    if (!dims.width || !dims.height) {
      throw new Error("decoder returned no dimensions");
    }
    const swapAxes = AXIS_SWAPPING_ORIENTATIONS.has(dims.orientation ?? 1);
    decodedWidth = swapAxes ? dims.height : dims.width;
    decodedHeight = swapAxes ? dims.width : dims.height;
  } catch (e) {
    return errorResponse(
      422,
      "unprocessable",
      `Could not decode image header: ${e instanceof Error ? e.message : "unknown error"}`,
    );
  }

  if (decodedType !== expectedType) {
    return errorResponse(
      422,
      "unprocessable",
      `Image content (${decodedType ?? "unknown"}) does not match its declared type '${storedMime}'`,
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
