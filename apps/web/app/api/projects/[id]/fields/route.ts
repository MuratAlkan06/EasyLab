import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { supabase } from "@/lib/supabase";
import { getWorkspaceId } from "@/lib/workspace";
import {
  conflict,
  internalError,
  notFound,
  unprocessable,
  validationError,
} from "@/lib/errors";

const ReferenceBoxSchema = z
  .object({
    x: z.number().min(0).max(1),
    y: z.number().min(0).max(1),
    width: z.number().gt(0).max(1),
    height: z.number().gt(0).max(1),
  })
  .refine((b) => b.x + b.width <= 1, {
    message: "reference_box.x + width must be <= 1",
  })
  .refine((b) => b.y + b.height <= 1, {
    message: "reference_box.y + height must be <= 1",
  });

const ExpectedFormatSchema = z.object({
  type: z.enum(["number", "text", "boolean"]),
  unit: z.string().nullable().optional(),
});

const FieldSchema = z.object({
  field_name: z.string().min(1).max(100),
  reference_box: ReferenceBoxSchema,
  semantic_description: z.string().max(500).optional(),
  expected_format: ExpectedFormatSchema.optional(),
});

const RequestSchema = z.object({
  fields: z.array(FieldSchema).min(1).max(50),
});

export async function GET(
  _req: NextRequest,
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

  const { data: fields, error: fldErr } = await supabase
    .from("template_fields")
    .select(
      "id, field_name, display_order, reference_box, semantic_description, expected_format, updated_at"
    )
    .eq("project_id", projectId)
    .order("display_order", { ascending: true });
  if (fldErr) return internalError(fldErr.message);

  return NextResponse.json({ fields: fields ?? [] });
}

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
    .select("id, reference_image_id")
    .eq("id", projectId)
    .eq("workspace_id", workspaceId)
    .maybeSingle();
  if (projErr) return internalError(projErr.message);
  if (!project) return notFound("Project not found");

  if (!project.reference_image_id) {
    return unprocessable("Project has no reference image set");
  }

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

  // Uniqueness check on field_name within submitted array
  const names = parsed.data.fields.map((f) => f.field_name);
  if (new Set(names).size !== names.length) {
    return validationError("field_name must be unique within submitted array");
  }

  // Replace operation: delete all existing fields then insert new
  const { error: delErr } = await supabase
    .from("template_fields")
    .delete()
    .eq("project_id", projectId);
  if (delErr) return internalError(delErr.message);

  const rows = parsed.data.fields.map((f, idx) => ({
    project_id: projectId,
    field_name: f.field_name,
    display_order: idx,
    reference_box: f.reference_box,
    semantic_description: f.semantic_description ?? null,
    expected_format: f.expected_format ?? null,
  }));

  const { data: inserted, error: insErr } = await supabase
    .from("template_fields")
    .insert(rows)
    .select(
      "id, field_name, display_order, reference_box, semantic_description, expected_format"
    )
    .order("display_order", { ascending: true });
  if (insErr) return internalError(insErr.message);

  // Promote project to 'annotated' if at least one field saved
  if ((inserted?.length ?? 0) >= 1) {
    await supabase
      .from("projects")
      .update({ status: "annotated" })
      .eq("id", projectId)
      .eq("status", "draft");
  }

  return NextResponse.json({ fields: inserted ?? [] });
}
