import { NextResponse } from "next/server";

export function errorResponse(
  status: number,
  code: string,
  message: string
): NextResponse {
  return NextResponse.json({ error: { code, message } }, { status });
}

export const notFound = (msg = "Not found") =>
  errorResponse(404, "not_found", msg);
export const conflict = (msg: string) =>
  errorResponse(409, "conflict", msg);
export const unprocessable = (msg: string) =>
  errorResponse(422, "unprocessable", msg);
export const quotaExceeded = (msg: string) =>
  errorResponse(429, "quota_exceeded", msg);
export const validationError = (msg: string) =>
  errorResponse(400, "validation_error", msg);
export const internalError = (msg = "Internal server error") =>
  errorResponse(500, "internal_error", msg);
