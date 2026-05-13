import { describe, expect, it } from "vitest";
import { csvEscape, csvRow } from "./csv";

describe("csvEscape", () => {
  it("returns plain strings unquoted", () => {
    expect(csvEscape("hello")).toBe("hello");
  });

  it("returns empty string for null", () => {
    expect(csvEscape(null)).toBe("");
  });

  it("returns empty string for undefined", () => {
    expect(csvEscape(undefined)).toBe("");
  });

  it("returns empty string for empty input", () => {
    expect(csvEscape("")).toBe("");
  });

  it("stringifies numbers without quoting", () => {
    expect(csvEscape(12.3)).toBe("12.3");
    expect(csvEscape(0)).toBe("0");
    expect(csvEscape(-7)).toBe("-7");
  });

  it("stringifies booleans without quoting", () => {
    expect(csvEscape(true)).toBe("true");
    expect(csvEscape(false)).toBe("false");
  });

  it("quotes values containing commas", () => {
    expect(csvEscape("a,b")).toBe('"a,b"');
  });

  it("quotes and doubles embedded quotes", () => {
    expect(csvEscape('he said "hi"')).toBe('"he said ""hi"""');
  });

  it("quotes values containing LF", () => {
    expect(csvEscape("a\nb")).toBe('"a\nb"');
  });

  it("quotes values containing CRLF", () => {
    expect(csvEscape("a\r\nb")).toBe('"a\r\nb"');
  });

  it("JSON-stringifies and quotes objects", () => {
    expect(csvEscape({ a: 1 })).toBe('"{""a"":1}"');
  });

  it("survives non-serializable values without throwing", () => {
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    expect(() => csvEscape(circular)).not.toThrow();
  });
});

describe("csvRow", () => {
  it("joins cells with commas", () => {
    expect(csvRow(["image_a.jpg", 12.3, "0.91", "ok"])).toBe(
      "image_a.jpg,12.3,0.91,ok"
    );
  });

  it("preserves [FAILED] literal for failed cells", () => {
    expect(csvRow(["image_b.jpg", "[FAILED]", "0.00", "failed"])).toBe(
      "image_b.jpg,[FAILED],0.00,failed"
    );
  });

  it("preserves * suffix for needs_review confidence", () => {
    expect(csvRow(["c.jpg", "v", "0.38*", "needs_review"])).toBe(
      "c.jpg,v,0.38*,needs_review"
    );
  });

  it("quotes only the cell that needs escaping", () => {
    expect(csvRow(["c.jpg", "value, with comma", "0.38*", "needs_review"])).toBe(
      'c.jpg,"value, with comma",0.38*,needs_review'
    );
  });
});
