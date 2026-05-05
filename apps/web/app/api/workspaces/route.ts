import { NextResponse } from "next/server";
import { supabase } from "@/lib/supabase";
import { cookies } from "next/headers";
import { internalError } from "@/lib/errors";

const COOKIE_OPTIONS = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
  maxAge: 60 * 60 * 24 * 365,
};

export async function POST() {
  const { data, error } = await supabase
    .from("workspaces")
    .insert({})
    .select("id")
    .single();

  if (error) return internalError(error.message);

  const cookieStore = await cookies();
  cookieStore.set("workspace_id", data.id, COOKIE_OPTIONS);

  return NextResponse.json({ workspace_id: data.id }, { status: 201 });
}
