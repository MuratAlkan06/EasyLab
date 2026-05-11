import { NextRequest, NextResponse } from "next/server";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import { internalError, notFound } from "@/lib/errors";

const BUCKET = "easylab";
const SIGNED_URL_TTL_SECONDS = 3600;

type ImageRow = {
  id: string;
  filename: string;
  storage_path: string | null;
  thumbnail_path: string | null;
};

type FieldRow = {
  id: string;
  field_name: string;
  display_order: number;
};

type CellRow = {
  id: string;
  image_id: string;
  field_id: string;
  raw_text: string | null;
  parsed_value: unknown;
  unit_seen: string | null;
  combined_confidence: number | null;
  status: "ok" | "low_confidence" | "needs_review" | "failed";
  failure_reason: string | null;
  validation_error: string | null;
  detected_box: unknown;
  crop_path: string | null;
};

type OverrideRow = {
  image_id: string;
  field_id: string;
  corrected_value: unknown;
  corrected_at: string;
};

async function signPath(path: string | null): Promise<string | null> {
  if (!path) return null;
  const { data } = await supabase.storage
    .from(BUCKET)
    .createSignedUrl(path, SIGNED_URL_TTL_SECONDS);
  return data?.signedUrl ?? null;
}

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: projectId } = await params;
  const workspaceId = await getWorkspaceId();
  if (!workspaceId) return notFound("No workspace");

  // Verify project ownership
  const { data: project, error: projErr } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .eq("workspace_id", workspaceId)
    .maybeSingle();
  if (projErr) return internalError(projErr.message);
  if (!project) return notFound("Project not found");

  // Latest succeeded job for this project (finished_at desc, fallback created_at desc)
  const { data: job, error: jobErr } = await supabase
    .from("jobs")
    .select("id, finished_at, created_at")
    .eq("project_id", projectId)
    .eq("status", "succeeded")
    .order("finished_at", { ascending: false, nullsFirst: false })
    .order("created_at", { ascending: false })
    .limit(1)
    .maybeSingle();
  if (jobErr) return internalError(jobErr.message);
  if (!job) return notFound("No completed job for this project");

  // Template fields (column order)
  const { data: fields, error: fldErr } = await supabase
    .from("template_fields")
    .select("id, field_name, display_order")
    .eq("project_id", projectId)
    .order("display_order", { ascending: true });
  if (fldErr) return internalError(fldErr.message);

  // Images that have cells in this job (rows of the table).
  // We restrict to images referenced by cells of this job to avoid showing
  // images that weren't part of the job (e.g. uploaded after the run).
  const { data: cells, error: cellErr } = await supabase
    .from("cells")
    .select(
      "id, image_id, field_id, raw_text, parsed_value, unit_seen, combined_confidence, status, failure_reason, validation_error, detected_box, crop_path"
    )
    .eq("job_id", job.id);
  if (cellErr) return internalError(cellErr.message);

  const cellsByImage = new Map<string, CellRow[]>();
  for (const c of (cells ?? []) as CellRow[]) {
    const arr = cellsByImage.get(c.image_id) ?? [];
    arr.push(c);
    cellsByImage.set(c.image_id, arr);
  }

  const imageIds = Array.from(cellsByImage.keys());
  let images: ImageRow[] = [];
  if (imageIds.length > 0) {
    const { data: imgs, error: imgErr } = await supabase
      .from("images")
      .select("id, filename, storage_path, thumbnail_path")
      .in("id", imageIds)
      .order("filename", { ascending: true });
    if (imgErr) return internalError(imgErr.message);
    images = (imgs ?? []) as ImageRow[];
  }

  // Cell overrides for this project (LEFT JOIN by (project_id, image_id, field_id))
  const { data: overrides, error: ovrErr } = await supabase
    .from("cell_overrides")
    .select("image_id, field_id, corrected_value, corrected_at")
    .eq("project_id", projectId);
  if (ovrErr) return internalError(ovrErr.message);

  const overrideMap = new Map<string, OverrideRow>();
  for (const o of (overrides ?? []) as OverrideRow[]) {
    overrideMap.set(`${o.image_id}:${o.field_id}`, o);
  }

  const fieldOrder = new Map<string, number>();
  for (const f of (fields ?? []) as FieldRow[]) {
    fieldOrder.set(f.id, f.display_order);
  }

  // Build rows
  const rows = await Promise.all(
    images.map(async (img) => {
      const signed_url = await signPath(img.thumbnail_path ?? img.storage_path);

      const imgCells = (cellsByImage.get(img.id) ?? []).slice();
      imgCells.sort((a, b) => {
        const ai = fieldOrder.get(a.field_id) ?? 0;
        const bi = fieldOrder.get(b.field_id) ?? 0;
        return ai - bi;
      });

      const cellsOut = await Promise.all(
        imgCells.map(async (c) => {
          const ovr = overrideMap.get(`${img.id}:${c.field_id}`) ?? null;
          const crop_signed_url = await signPath(c.crop_path);
          return {
            id: c.id,
            field_id: c.field_id,
            raw_text: c.raw_text,
            parsed_value: c.parsed_value,
            unit_seen: c.unit_seen,
            combined_confidence:
              c.combined_confidence == null
                ? null
                : Number(c.combined_confidence),
            status: c.status,
            failure_reason: c.failure_reason,
            validation_error: c.validation_error,
            corrected_value: ovr ? ovr.corrected_value : null,
            corrected_at: ovr ? ovr.corrected_at : null,
            detected_box: c.detected_box,
            crop_path: c.crop_path,
            crop_signed_url,
          };
        })
      );

      return {
        image_id: img.id,
        filename: img.filename,
        signed_url,
        cells: cellsOut,
      };
    })
  );

  return NextResponse.json({
    job_id: job.id,
    fields: (fields ?? []) as FieldRow[],
    rows,
  });
}
