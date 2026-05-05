import { cookies } from "next/headers";
import { supabase } from "./supabase";

const COOKIE_NAME = "workspace_id";
const COOKIE_OPTIONS = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
  maxAge: 60 * 60 * 24 * 365, // 1 year
};

export async function getOrCreateWorkspace(): Promise<string> {
  const cookieStore = await cookies();
  const existing = cookieStore.get(COOKIE_NAME)?.value;
  if (existing) return existing;

  const { data, error } = await supabase
    .from("workspaces")
    .insert({})
    .select("id")
    .single();

  if (error) throw new Error(`Failed to create workspace: ${error.message}`);

  cookieStore.set(COOKIE_NAME, data.id, COOKIE_OPTIONS);
  return data.id;
}

export async function getWorkspaceId(): Promise<string | null> {
  const cookieStore = await cookies();
  return cookieStore.get(COOKIE_NAME)?.value ?? null;
}
