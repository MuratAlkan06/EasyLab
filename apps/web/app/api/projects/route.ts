import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { supabase } from "@/lib/supabase";
import { getOrCreateWorkspace } from "@/lib/workspace";
import { conflict, internalError, validationError } from "@/lib/errors";

const CreateProjectSchema = z.object({
  name: z.string().min(1).max(100),
});

export async function GET() {
  const workspaceId = await getOrCreateWorkspace();

  const { data, error } = await supabase
    .from("projects")
    .select("id, name, status, created_at, updated_at")
    .eq("workspace_id", workspaceId)
    .order("created_at", { ascending: false });

  if (error) return internalError(error.message);

  return NextResponse.json({ projects: data });
}

export async function POST(req: NextRequest) {
  const workspaceId = await getOrCreateWorkspace();

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return validationError("Invalid JSON body");
  }

  const parsed = CreateProjectSchema.safeParse(body);
  if (!parsed.success) {
    return validationError(parsed.error.issues[0].message);
  }

  const { data, error } = await supabase
    .from("projects")
    .insert({ workspace_id: workspaceId, name: parsed.data.name })
    .select("id, name, status, created_at, updated_at")
    .single();

  if (error) {
    if (error.code === "23505") {
      return conflict(`A project named "${parsed.data.name}" already exists`);
    }
    return internalError(error.message);
  }

  return NextResponse.json({ project: data }, { status: 201 });
}
