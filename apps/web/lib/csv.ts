const NEEDS_QUOTING = /[",\r\n]/;

export function csvEscape(value: unknown): string {
  if (value === null || value === undefined) return "";
  let s: string;
  if (typeof value === "string") s = value;
  else if (typeof value === "number" || typeof value === "boolean") s = String(value);
  else {
    try {
      s = JSON.stringify(value);
    } catch {
      s = "";
    }
  }
  if (!NEEDS_QUOTING.test(s)) return s;
  return `"${s.replace(/"/g, '""')}"`;
}

export function csvRow(cells: unknown[]): string {
  return cells.map(csvEscape).join(",");
}
