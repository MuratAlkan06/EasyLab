/**
 * Tests for POST /api/projects/[id]/images/[imageId]/confirm — the
 * server-trusted width_px/height_px written on upload confirm.
 *
 * width_px/height_px must be UPRIGHT dimensions (EXIF orientation applied),
 * the space users annotate in and the worker crops in (ADR-007 Slice 2 / A4).
 * The Supabase client and workspace cookie helper are mocked at the module
 * level; the real `image-size` decoder runs against tiny synthetic JPEG
 * headers, so the orientation handling is exercised end to end.
 *
 * The route also only lets image-size parse JPEG/PNG and requires the decoded
 * type to match the stored mime (CVE-2025-71329/71330 hardening).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { imageSize } from "image-size";

const mockState = vi.hoisted(() => ({
  workspaceId: "workspace-1" as string | null,
  fileBytes: new Uint8Array(0),
  mimeType: "image/jpeg",
  // Overrides the stored size list() reports, so the size limit can be
  // exercised without allocating ~20 MB; null means fileBytes.length.
  storedSize: null as number | null,
  downloads: 0,
  updates: [] as Record<string, unknown>[],
}));

vi.mock("@/lib/workspace", () => ({
  getWorkspaceId: vi.fn(async () => mockState.workspaceId),
}));

vi.mock("@/lib/supabase", () => {
  const rows: Record<string, unknown> = {
    projects: { id: "p1" },
    images: {
      id: "img-1",
      status: "pending_upload",
      storage_path: "projects/p1/originals/img-1.jpg",
    },
  };
  function makeBuilder(table: string) {
    const builder: Record<string, unknown> = {
      select: () => builder,
      eq: () => builder,
      update: (values: Record<string, unknown>) => {
        mockState.updates.push(values);
        return builder;
      },
      maybeSingle: async () => ({ data: rows[table] ?? null, error: null }),
      then(onFulfilled: (v: unknown) => unknown, onRejected: (e: unknown) => unknown) {
        return Promise.resolve({ error: null }).then(onFulfilled, onRejected);
      },
    };
    return builder;
  }
  return {
    supabase: {
      from: (table: string) => makeBuilder(table),
      storage: {
        from: () => ({
          list: async () => ({
            data: [
              {
                metadata: {
                  mimetype: mockState.mimeType,
                  size: mockState.storedSize ?? mockState.fileBytes.length,
                },
              },
            ],
            error: null,
          }),
          download: async () => {
            mockState.downloads += 1;
            return { data: new Blob([mockState.fileBytes]), error: null };
          },
        }),
      },
    },
  };
});

import { POST } from "@/app/api/projects/[id]/images/[imageId]/confirm/route";

// Stored (RAW) pixel grid of every fixture: landscape, as a phone sensor writes it.
const RAW_W = 40;
const RAW_H = 24;

function u16(n: number, le: boolean): number[] {
  return le ? [n & 0xff, n >> 8] : [n >> 8, n & 0xff];
}

function u32(n: number, le: boolean): number[] {
  const be = [(n >>> 24) & 0xff, (n >>> 16) & 0xff, (n >>> 8) & 0xff, n & 0xff];
  return le ? be.reverse() : be;
}

/**
 * Minimal JPEG header: SOI, JFIF APP0, optional APP1 Exif whose IFD0 holds a
 * single Orientation (0x0112, SHORT) tag, a baseline SOF0 declaring the RAW
 * stored width/height, EOI. image-size only parses headers, so no scan data.
 */
function jpegHeader(orientation?: number, littleEndian = false): Uint8Array<ArrayBuffer> {
  const le = littleEndian;
  const bytes = [0xff, 0xd8];
  const jfif = [0x4a, 0x46, 0x49, 0x46, 0x00, 1, 1, 0, 0, 1, 0, 1, 0, 0];
  bytes.push(0xff, 0xe0, ...u16(jfif.length + 2, false), ...jfif);
  if (orientation !== undefined) {
    const tiff = [
      ...(le ? [0x49, 0x49] : [0x4d, 0x4d]), // "II" / "MM" byte order
      ...u16(42, le),
      ...u32(8, le), // IFD0 right after the TIFF header
      ...u16(1, le), // one entry
      ...u16(0x0112, le),
      ...u16(3, le), // SHORT
      ...u32(1, le), // count
      ...u16(orientation, le),
      0,
      0, // value, left-justified in its 4-byte slot
      ...u32(0, le), // no next IFD
    ];
    const app1 = [0x45, 0x78, 0x69, 0x66, 0x00, 0x00, ...tiff]; // "Exif\0\0"
    bytes.push(0xff, 0xe1, ...u16(app1.length + 2, false), ...app1);
  }
  const sof0 = [8, ...u16(RAW_H, false), ...u16(RAW_W, false), 1, 1, 0x11, 0];
  bytes.push(0xff, 0xc0, ...u16(sof0.length + 2, false), ...sof0);
  bytes.push(0xff, 0xd9);
  return new Uint8Array(bytes);
}

function ascii(s: string): number[] {
  return Array.from(s, (c) => c.charCodeAt(0));
}

/** Minimal PNG header: signature + IHDR declaring RAW_W×RAW_H (CRC unchecked). */
function pngHeader(): Uint8Array<ArrayBuffer> {
  const ihdr = [...u32(RAW_W, false), ...u32(RAW_H, false), 8, 2, 0, 0, 0];
  return new Uint8Array([
    0x89,
    ...ascii("PNG\r\n\x1a\n"),
    ...u32(ihdr.length, false),
    ...ascii("IHDR"),
    ...ihdr,
    ...u32(0, false),
  ]);
}

/**
 * Headers of formats the product does not accept, each valid enough for
 * image-size's detector to identify. The ICNS/HEIF/JXL ones carry the
 * zero-length entry/box shape behind CVE-2025-71329/71330: a parser that
 * advances by the declared length never moves past it.
 */
const FOREIGN_HEADERS: Record<string, number[]> = {
  icns: [...ascii("icns"), ...u32(16, false), ...ascii("ic08"), ...u32(0, false)],
  heif: [
    ...u32(16, false),
    ...ascii("ftypheic"),
    ...u32(0, false),
    ...u32(0, false),
    ...ascii("meta"),
  ],
  jxl: [
    ...u32(12, false),
    ...ascii("JXL "),
    0x0d,
    0x0a,
    0x87,
    0x0a,
    ...u32(20, false),
    ...ascii("ftypjxl "),
    ...u32(0, false),
    ...ascii("jxl "),
    ...u32(0, false),
    ...ascii("jxlp"),
  ],
  // Well-formed and harmless, but still not JPEG/PNG: its parser is off too.
  gif: [...ascii("GIF89a"), ...u16(RAW_W, true), ...u16(RAW_H, true), 0, 0, 0],
};

async function confirm(fileBytes: Uint8Array<ArrayBuffer>, mimeType = "image/jpeg") {
  mockState.fileBytes = fileBytes;
  mockState.mimeType = mimeType;
  const req = new Request("http://localhost/api/projects/p1/images/img-1/confirm", {
    method: "POST",
  }) as unknown as Parameters<typeof POST>[0];
  return POST(req, { params: Promise.resolve({ id: "p1", imageId: "img-1" }) });
}

beforeEach(() => {
  mockState.workspaceId = "workspace-1";
  mockState.fileBytes = new Uint8Array(0);
  mockState.mimeType = "image/jpeg";
  mockState.storedSize = null;
  mockState.downloads = 0;
  mockState.updates = [];
});

describe("POST /api/projects/[id]/images/[imageId]/confirm", () => {
  it("fixture premise: image-size reports RAW dims plus an unapplied orientation", () => {
    expect(imageSize(jpegHeader(6))).toMatchObject({
      width: RAW_W,
      height: RAW_H,
      orientation: 6,
    });
  });

  it("stores transposed (upright) dims for an orientation-6 JPEG", async () => {
    const res = await confirm(jpegHeader(6));

    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ width_px: RAW_H, height_px: RAW_W });
    expect(mockState.updates).toHaveLength(1);
    expect(mockState.updates[0]).toMatchObject({
      status: "uploaded",
      width_px: RAW_H,
      height_px: RAW_W,
    });
  });

  it("handles little-endian EXIF the same way", async () => {
    const res = await confirm(jpegHeader(6, true));
    expect(await res.json()).toMatchObject({ width_px: RAW_H, height_px: RAW_W });
  });

  it("leaves dims unchanged when there is no EXIF orientation", async () => {
    const res = await confirm(jpegHeader());

    expect(res.status).toBe(200);
    expect(mockState.updates[0]).toMatchObject({ width_px: RAW_W, height_px: RAW_H });
  });

  it.each([
    [1, false],
    [2, false],
    [3, false],
    [4, false],
    [5, true],
    [6, true],
    [7, true],
    [8, true],
  ])("orientation %i swaps axes: %s", async (orientation, swapped) => {
    const res = await confirm(jpegHeader(orientation));

    expect(res.status).toBe(200);
    const [w, h] = swapped ? [RAW_H, RAW_W] : [RAW_W, RAW_H];
    expect(mockState.updates[0]).toMatchObject({ width_px: w, height_px: h });
  });

  it("accepts a PNG declared as image/png", async () => {
    const res = await confirm(pngHeader(), "image/png");

    expect(res.status).toBe(200);
    expect(mockState.updates[0]).toMatchObject({
      mime_type: "image/png",
      width_px: RAW_W,
      height_px: RAW_H,
    });
  });

  it.each(Object.keys(FOREIGN_HEADERS))(
    "rejects a crafted %s file at detection, before its parser runs",
    async (type) => {
      const res = await confirm(new Uint8Array(FOREIGN_HEADERS[type]));

      expect(res.status).toBe(422);
      // "disabled file type" is thrown by image-size's lookup before the
      // format's calculate() is entered, so the vulnerable parser never runs.
      expect(await res.json()).toEqual({
        error: {
          code: "unprocessable",
          message: `Could not decode image header: disabled file type: ${type}`,
        },
      });
      expect(mockState.updates).toHaveLength(0);
    },
  );

  it.each([
    ["PNG bytes declared as image/jpeg", pngHeader, "image/jpeg", "png"],
    ["JPEG bytes declared as image/png", () => jpegHeader(), "image/png", "jpg"],
  ])("rejects %s", async (_label, bytes, declaredMime, decodedType) => {
    const res = await confirm(bytes(), declaredMime);

    expect(res.status).toBe(422);
    expect(await res.json()).toEqual({
      error: {
        code: "unprocessable",
        message: `Image content (${decodedType}) does not match its declared type '${declaredMime}'`,
      },
    });
    expect(mockState.updates).toHaveLength(0);
  });

  it.each(["image/webp", "image/tiff", "image/heic"])(
    "rejects stored mime %s before downloading the file",
    async (mime) => {
      const res = await confirm(jpegHeader(), mime);

      expect(res.status).toBe(422);
      expect(await res.json()).toEqual({
        error: {
          code: "unprocessable",
          message: `Unsupported image type '${mime}'. Allowed: JPEG, PNG.`,
        },
      });
      expect(mockState.downloads).toBe(0);
      expect(mockState.updates).toHaveLength(0);
    },
  );

  it("rejects a stored object over the 20 MB upload-urls limit before downloading it", async () => {
    mockState.storedSize = 20_971_521;
    const res = await confirm(jpegHeader());

    expect(res.status).toBe(422);
    expect(await res.json()).toEqual({
      error: { code: "unprocessable", message: "File exceeds maximum size of 20 MB" },
    });
    expect(mockState.downloads).toBe(0);
    expect(mockState.updates).toHaveLength(0);

    // Boundary: exactly 20 MB is still accepted.
    mockState.storedSize = 20_971_520;
    const ok = await confirm(jpegHeader());

    expect(ok.status).toBe(200);
    expect(mockState.updates[0]).toMatchObject({ status: "uploaded", size_bytes: 20_971_520 });
  });
});
