import { NextRequest, NextResponse } from "next/server";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import { internalError, notFound } from "@/lib/errors";
import { csvRow } from "@/lib/csv";

type CellStatus = "ok" | "low_confidence" | "needs_review" | "failed";

type FieldRow = {
  id: string;
  field_name: string;
  display_order: number;
};

type CellRow = {
  image_id: string;
  field_id: string;
  raw_text: string | null;
  parsed_value: unknown;
  combined_confidence: number | null;
  status: CellStatus;
};

type ImageRow = {
  id: string;
  filename: string;
};

type OverrideRow = {
  image_id: string;
  field_id: string;
  corrected_value: unknown;
};

function sanitizeFilenamePart(s: string): string {
  return s.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "project";
}

function todayYmd(): string {
  const d = new Date();
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function effectiveValue(cell: CellRow, override: OverrideRow | undefined): unknown {
  if (override && override.corrected_value !== null && override.corrected_value !== undefined) {
    return override.corrected_value;
  }
  if (cell.parsed_value !== null && cell.parsed_value !== undefined) return cell.parsed_value;
  return cell.raw_text;
}

function confidenceCell(cell: CellRow): string {
  const c = cell.combined_confidence == null ? 0 : Number(cell.combined_confidence);
  const rounded = c.toFixed(2);
  return cell.status === "needs_review" ? `${rounded}*` : rounded;
}

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: projectId } = await params;
  const workspaceId = await getWorkspaceId();
  if (!workspaceId) return notFound("No workspace");

  const url = new URL(req.url);
  const explicitJobId = url.searchParams.get("job_id");

  // Verify project ownership and grab name for filename
  const { data: project, error: projErr } = await supabase
    .from("projects")
    .select("id, name")
    .eq("id", projectId)
    .eq("workspace_id", workspaceId)
    .maybeSingle();
  if (projErr) return internalError(projErr.message);
  if (!project) return notFound("Project not found");

  // Resolve job: explicit or latest succeeded
  let jobId: string;
  if (explicitJobId) {
    const { data: job, error: jobErr } = await supabase
      .from("jobs")
      .select("id")
      .eq("id", explicitJobId)
      .eq("project_id", projectId)
      .maybeSingle();
    if (jobErr) return internalError(jobErr.message);
    if (!job) return notFound("Job not found");
    jobId = job.id;
  } else {
    const { data: job, error: jobErr } = await supabase
      .from("jobs")
      .select("id")
      .eq("project_id", projectId)
      .eq("status", "succeeded")
      .order("finished_at", { ascending: false, nullsFirst: false })
      .order("created_at", { ascending: false })
      .limit(1)
      .maybeSingle();
    if (jobErr) return internalError(jobErr.message);
    if (!job) return notFound("No completed job for this project");
    jobId = job.id;
  }

  // Template fields in display_order
  const { data: fields, error: fldErr } = await supabase
    .from("template_fields")
    .select("id, field_name, display_order")
    .eq("project_id", projectId)
    .order("display_order", { ascending: true });
  if (fldErr) return internalError(fldErr.message);
  const fieldList = (fields ?? []) as FieldRow[];

  // Cells for this job
  const { data: cellsData, error: cellErr } = await supabase
    .from("cells")
    .select("image_id, field_id, raw_text, parsed_value, combined_confidence, status")
    .eq("job_id", jobId);
  if (cellErr) return internalError(cellErr.message);
  const cells = (cellsData ?? []) as CellRow[];

  // Cell overrides (project-scoped — survive re-runs)
  const { data: overridesData, error: ovrErr } = await supabase
    .from("cell_overrides")
    .select("image_id, field_id, corrected_value")
    .eq("project_id", projectId);
  if (ovrErr) return internalError(ovrErr.message);
  const overrideMap = new Map<string, OverrideRow>();
  for (const o of (overridesData ?? []) as OverrideRow[]) {
    overrideMap.set(`${o.image_id}:${o.field_id}`, o);
  }

  // Images referenced by cells, ordered by filename
  const imageIds = Array.from(new Set(cells.map((c) => c.image_id)));
  let images: ImageRow[] = [];
  if (imageIds.length > 0) {
    const { data: imgs, error: imgErr } = await supabase
      .from("images")
      .select("id, filename")
      .in("id", imageIds)
      .order("filename", { ascending: true });
    if (imgErr) return internalError(imgErr.message);
    images = (imgs ?? []) as ImageRow[];
  }

  // Pivot: (image_id, field_id) → cell
  const cellMap = new Map<string, CellRow>();
  for (const c of cells) {
    cellMap.set(`${c.image_id}:${c.field_id}`, c);
  }

  // Header row
  const header: string[] = ["filename"];
  for (const f of fieldList) {
    header.push(f.field_name, `${f.field_name}_confidence`, `${f.field_name}_status`);
  }

  const lines: string[] = [];
  lines.push(csvRow(header));

  // Data rows
  for (const img of images) {
    const row: unknown[] = [img.filename];
    for (const f of fieldList) {
      const cell = cellMap.get(`${img.id}:${f.id}`);
      if (!cell) {
        row.push("", "0.00", "missing");
        continue;
      }
      const override = overrideMap.get(`${img.id}:${f.id}`);
      const isCorrected =
        override && override.corrected_value !== null && override.corrected_value !== undefined;

      let valueOut: unknown;
      if (cell.status === "failed" && !isCorrected) {
        valueOut = "[FAILED]";
      } else {
        valueOut = effectiveValue(cell, override);
      }
      row.push(valueOut, confidenceCell(cell), cell.status);
    }
    lines.push(csvRow(row));
  }

  // RFC 4180 line terminator; BOM helps Excel detect UTF-8
  const body = "﻿" + lines.join("\r\n") + "\r\n";
  const filename = `easylab-${sanitizeFilenamePart(project.name)}-${todayYmd()}.csv`;

  return new NextResponse(body, {
    status: 200,
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="${filename}"`,
      "Cache-Control": "no-store",
    },
  });
}

