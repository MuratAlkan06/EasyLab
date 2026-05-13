/**
 * Integration-shaped tests for GET /api/projects/[id]/export.csv.
 *
 * The Supabase client and workspace cookie helper are mocked at the module
 * level — everything else (route logic, CSV escaping, status pivoting,
 * override precedence) runs as-is. Asserts against the exact bytes returned
 * by the handler.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const mockState = vi.hoisted(() => ({
  workspaceId: "workspace-1" as string | null,
  tables: {} as Record<string, { data: unknown; error: { message: string } | null }>,
}));

vi.mock("@/lib/workspace", () => ({
  getWorkspaceId: vi.fn(async () => mockState.workspaceId),
}));

vi.mock("@/lib/supabase", () => {
  function makeBuilder(table: string) {
    const builder: Record<string, unknown> = {
      select: () => builder,
      eq: () => builder,
      in: () => builder,
      order: () => builder,
      limit: () => builder,
      maybeSingle: async () => mockState.tables[table] ?? { data: null, error: null },
      then(onFulfilled: (v: unknown) => unknown, onRejected: (e: unknown) => unknown) {
        const result = mockState.tables[table] ?? { data: [], error: null };
        return Promise.resolve(result).then(onFulfilled, onRejected);
      },
    };
    return builder;
  }
  return {
    supabase: { from: (table: string) => makeBuilder(table) },
  };
});

import { GET } from "@/app/api/projects/[id]/export.csv/route";

function makeReq(path = "/api/projects/p1/export.csv") {
  return new Request(`http://localhost${path}`) as unknown as Parameters<typeof GET>[0];
}

function makeParams(id: string) {
  return { params: Promise.resolve({ id }) };
}

function setTables(overrides: Record<string, unknown>) {
  mockState.tables = {} as typeof mockState.tables;
  for (const [table, data] of Object.entries(overrides)) {
    mockState.tables[table] = { data, error: null };
  }
}

beforeEach(() => {
  mockState.workspaceId = "workspace-1";
  mockState.tables = {} as typeof mockState.tables;
});

describe("GET /api/projects/[id]/export.csv", () => {
  it("returns 404 when no workspace cookie", async () => {
    mockState.workspaceId = null;
    const res = await GET(makeReq(), makeParams("p1"));
    expect(res.status).toBe(404);
  });

  it("returns 404 when project not in workspace", async () => {
    setTables({ projects: null });
    const res = await GET(makeReq(), makeParams("p1"));
    expect(res.status).toBe(404);
  });

  it("returns 404 when no completed job", async () => {
    setTables({
      projects: { id: "p1", name: "X" },
      jobs: null,
    });
    const res = await GET(makeReq(), makeParams("p1"));
    expect(res.status).toBe(404);
  });

  it("emits a header row plus one row per image with three columns per field", async () => {
    setTables({
      projects: { id: "p1", name: "Phase 3 Lab" },
      jobs: { id: "job-1" },
      template_fields: [
        { id: "f-volt", field_name: "voltage", display_order: 0 },
        { id: "f-curr", field_name: "current", display_order: 1 },
      ],
      cells: [
        {
          image_id: "i-a",
          field_id: "f-volt",
          raw_text: "12.3",
          parsed_value: 12.3,
          combined_confidence: 0.91,
          status: "ok",
        },
        {
          image_id: "i-a",
          field_id: "f-curr",
          raw_text: "2.1",
          parsed_value: 2.1,
          combined_confidence: 0.72,
          status: "low_confidence",
        },
        {
          image_id: "i-b",
          field_id: "f-volt",
          raw_text: null,
          parsed_value: null,
          combined_confidence: 0,
          status: "failed",
        },
        {
          image_id: "i-b",
          field_id: "f-curr",
          raw_text: "N/A",
          parsed_value: "N/A",
          combined_confidence: 0.38,
          status: "needs_review",
        },
      ],
      cell_overrides: [
        { image_id: "i-a", field_id: "f-curr", corrected_value: 2.15 },
      ],
      images: [
        { id: "i-a", filename: "image_a.jpg" },
        { id: "i-b", filename: "image_b.jpg" },
      ],
    });

    const res = await GET(makeReq(), makeParams("p1"));
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("text/csv; charset=utf-8");
    expect(res.headers.get("Content-Disposition") ?? "").toMatch(
      /attachment; filename="easylab-Phase-3-Lab-\d{4}-\d{2}-\d{2}\.csv"/,
    );

    const body = await res.text();
    // Strip UTF-8 BOM the route prepends for Excel
    const csv = body.replace(/^﻿/, "");
    const lines = csv.trim().split("\r\n");

    expect(lines[0]).toBe(
      "filename,voltage,voltage_confidence,voltage_status,current,current_confidence,current_status",
    );

    // image_a: voltage ok 12.3 / current corrected to 2.15 (status stays low_confidence)
    expect(lines[1]).toBe("image_a.jpg,12.3,0.91,ok,2.15,0.72,low_confidence");

    // image_b: voltage failed → [FAILED] / current needs_review → * suffix on confidence
    expect(lines[2]).toBe("image_b.jpg,[FAILED],0.00,failed,N/A,0.38*,needs_review");
  });

  it("CSV-escapes commas in field names and values", async () => {
    setTables({
      projects: { id: "p1", name: "Csv, Edge Case" },
      jobs: { id: "job-1" },
      template_fields: [
        { id: "f-1", field_name: "voltage, peak", display_order: 0 },
      ],
      cells: [
        {
          image_id: "i-1",
          field_id: "f-1",
          raw_text: 'he said "12"',
          parsed_value: 'he said "12"',
          combined_confidence: 0.9,
          status: "ok",
        },
      ],
      cell_overrides: [],
      images: [{ id: "i-1", filename: "a,b.jpg" }],
    });

    const res = await GET(makeReq(), makeParams("p1"));
    const csv = (await res.text()).replace(/^﻿/, "");
    const lines = csv.trim().split("\r\n");

    expect(lines[0]).toBe(
      '"voltage, peak","voltage, peak_confidence","voltage, peak_status"'.replace(
        /^/,
        "filename,",
      ),
    );
    expect(lines[1]).toBe('"a,b.jpg","he said ""12""",0.90,ok');
  });

  it("honours ?job_id query param", async () => {
    setTables({
      projects: { id: "p1", name: "X" },
      jobs: { id: "job-pinned" },
      template_fields: [],
      cells: [],
      cell_overrides: [],
      images: [],
    });

    const res = await GET(
      makeReq("/api/projects/p1/export.csv?job_id=job-pinned"),
      makeParams("p1"),
    );
    expect(res.status).toBe(200);
  });
});
