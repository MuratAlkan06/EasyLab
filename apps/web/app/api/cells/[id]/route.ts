import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import {
  internalError,
  notFound,
  validationError,
} from "@/lib/errors";

const RequestSchema = z.object({
  // Allow primitives and null; server validates against expected_format below.
  corrected_value: z.union([z.string(), z.number(), z.boolean(), z.null()]),
});

type ExpectedFormat = {
  type: "number" | "text" | "boolean";
  unit?: string | null;
} | null;

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id: cellId } = await params;
  const workspaceId = await getWorkspaceId();
  if (!workspaceId) return notFound("No workspace");

  // Load cell with project and field; verify workspace via project
  const { data: cell, error: cellErr } = await supabase
    .from("cells")
    .select(
      "id, project_id, image_id, field_id, projects!inner(id, workspace_id), template_fields!inner(id, expected_format)"
    )
    .eq("id", cellId)
    .maybeSingle();

  if (cellErr) return internalError(cellErr.message);
  if (!cell) return notFound("Cell not found");

  const project = cell.projects as unknown as {
    id: string;
    workspace_id: string;
  } | null;
  if (!project || project.workspace_id !== workspaceId) {
    return notFound("Cell not found");
  }

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

  const value = parsed.data.corrected_value;

  // Type validation against template_fields.expected_format
  const tf = cell.template_fields as unknown as {
    expected_format: ExpectedFormat;
  };
  const expected = tf?.expected_format ?? null;
  const expectedType = expected?.type ?? "text";

  if (value !== null) {
    if (expectedType === "number" && typeof value !== "number") {
      return validationError("corrected_value must be a number");
    }
    if (expectedType === "boolean" && typeof value !== "boolean") {
      return validationError("corrected_value must be a boolean");
    }
    if (expectedType === "text" && typeof value !== "string") {
      return validationError("corrected_value must be a string");
    }
  }

  if (value === null) {
    const { error: delErr } = await supabase
      .from("cell_overrides")
      .delete()
      .eq("project_id", cell.project_id)
      .eq("image_id", cell.image_id)
      .eq("field_id", cell.field_id);
    if (delErr) return internalError(delErr.message);

    return NextResponse.json({
      id: cellId,
      corrected_value: null,
      corrected_at: null,
    });
  }

  const now = new Date().toISOString();
  const { data: upserted, error: upsertErr } = await supabase
    .from("cell_overrides")
    .upsert(
      {
        project_id: cell.project_id,
        image_id: cell.image_id,
        field_id: cell.field_id,
        corrected_value: value,
        corrected_at: now,
      },
      { onConflict: "project_id,image_id,field_id" }
    )
    .select("corrected_value, corrected_at")
    .single();
  if (upsertErr) return internalError(upsertErr.message);

  return NextResponse.json({
    id: cellId,
    corrected_value: upserted?.corrected_value ?? value,
    corrected_at: upserted?.corrected_at ?? now,
  });
}
