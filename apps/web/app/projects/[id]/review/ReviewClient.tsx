"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import {
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ColumnDef,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { toast } from "sonner";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Download,
  Loader2,
  Pencil,
  X,
} from "lucide-react";
import { Shell, Button, Badge } from "@/components/Shell";

type CellStatus = "ok" | "low_confidence" | "needs_review" | "failed";

type ApiField = {
  id: string;
  field_name: string;
  display_order: number;
};

type ApiCell = {
  id: string;
  field_id: string;
  raw_text: string | null;
  parsed_value: unknown;
  unit_seen: string | null;
  combined_confidence: number | null;
  status: CellStatus;
  failure_reason: string | null;
  validation_error: string | null;
  corrected_value: unknown;
  corrected_at: string | null;
  detected_box: unknown;
  crop_path: string | null;
  crop_signed_url: string | null;
};

type ApiRow = {
  image_id: string;
  filename: string;
  signed_url: string | null;
  cells: ApiCell[];
};

type CellsResponse = {
  job_id: string;
  fields: ApiField[];
  rows: ApiRow[];
};

const ROW_HEIGHT = 64;

const STATUS_RANK: Record<CellStatus, number> = {
  failed: 3,
  needs_review: 2,
  low_confidence: 1,
  ok: 0,
};

function isIssue(status: CellStatus): boolean {
  return (
    status === "low_confidence" ||
    status === "needs_review" ||
    status === "failed"
  );
}

function displayValue(cell: ApiCell): string {
  const v =
    cell.corrected_value !== null && cell.corrected_value !== undefined
      ? cell.corrected_value
      : cell.parsed_value !== null && cell.parsed_value !== undefined
      ? cell.parsed_value
      : cell.raw_text;
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return "";
  }
}

function sortValue(cell: ApiCell | undefined): string | number {
  if (!cell) return "";
  const v =
    cell.corrected_value !== null && cell.corrected_value !== undefined
      ? cell.corrected_value
      : cell.parsed_value !== null && cell.parsed_value !== undefined
      ? cell.parsed_value
      : cell.raw_text ?? "";
  if (typeof v === "number") return v;
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "string") return v.toLowerCase();
  return "";
}

export default function ReviewClient({ projectId }: { projectId: string }) {
  const qc = useQueryClient();

  const cellsQuery = useQuery({
    queryKey: ["cells", projectId],
    queryFn: async () => {
      const res = await fetch(`/api/projects/${projectId}/cells`);
      if (res.status === 404) {
        return { empty: true } as const;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error?.message ?? "Failed to load cells");
      }
      return (await res.json()) as CellsResponse;
    },
  });

  const data = cellsQuery.data;
  const isEmpty = !!data && "empty" in data && data.empty === true;
  const cellsData = !data || isEmpty ? null : (data as CellsResponse);

  const [issuesOnly, setIssuesOnly] = useState(false);
  const [sorting, setSorting] = useState<SortingState>([
    { id: "filename", desc: false },
  ]);

  const fields = useMemo(() => cellsData?.fields ?? [], [cellsData]);
  const rows = useMemo(() => cellsData?.rows ?? [], [cellsData]);

  // Per-row helpers
  const rowHasIssue = useCallback((row: ApiRow): boolean => {
    return row.cells.some(
      (c) =>
        isIssue(c.status) ||
        (c.corrected_value !== null && c.corrected_value !== undefined)
    );
  }, []);

  const filteredRows = useMemo(() => {
    if (!issuesOnly) return rows;
    return rows.filter(rowHasIssue);
  }, [rows, issuesOnly, rowHasIssue]);

  const totalIssueCount = useMemo(
    () => rows.reduce((acc, r) => (rowHasIssue(r) ? acc + 1 : acc), 0),
    [rows, rowHasIssue]
  );

  // Per-field issue counts (column header badge)
  const issueCountByField = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of rows) {
      for (const c of r.cells) {
        if (isIssue(c.status)) {
          m.set(c.field_id, (m.get(c.field_id) ?? 0) + 1);
        }
      }
    }
    return m;
  }, [rows]);

  // Inline edit state — keyed by cell id
  const [editingCellId, setEditingCellId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState<string>("");
  const [savingCellId, setSavingCellId] = useState<string | null>(null);
  const [zoomSrc, setZoomSrc] = useState<string | null>(null);

  const beginEdit = useCallback((cell: ApiCell) => {
    setEditingCellId(cell.id);
    setEditValue(displayValue(cell));
  }, []);

  const cancelEdit = useCallback(() => {
    setEditingCellId(null);
    setEditValue("");
  }, []);

  const commitEdit = useCallback(
    async (cell: ApiCell, fieldId: string) => {
      const trimmed = editValue.trim();
      // Build typed corrected_value. We don't have expected_format on the
      // client, so try number first if it parses cleanly, then boolean
      // 'true'/'false', else string. Empty string clears the override.
      let correctedValue: string | number | boolean | null;
      if (trimmed === "") {
        correctedValue = null;
      } else if (/^-?\d+(\.\d+)?$/.test(trimmed)) {
        correctedValue = Number(trimmed);
      } else if (trimmed === "true" || trimmed === "false") {
        correctedValue = trimmed === "true";
      } else {
        correctedValue = trimmed;
      }

      setSavingCellId(cell.id);
      try {
        const res = await fetch(`/api/cells/${cell.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ corrected_value: correctedValue }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
          // If number/text mismatch — retry as string
          if (
            res.status === 400 &&
            typeof correctedValue === "number" &&
            typeof body?.error?.message === "string" &&
            body.error.message.toLowerCase().includes("string")
          ) {
            const retry = await fetch(`/api/cells/${cell.id}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ corrected_value: trimmed }),
            });
            const retryBody = await retry.json().catch(() => ({}));
            if (!retry.ok) {
              throw new Error(
                retryBody?.error?.message ?? "Failed to save correction"
              );
            }
            applyOverrideToCache(cell.id, fieldId, retryBody);
          } else {
            throw new Error(body?.error?.message ?? "Failed to save correction");
          }
        } else {
          applyOverrideToCache(cell.id, fieldId, body);
        }
        toast.success("Correction saved");
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Failed to save");
      } finally {
        setSavingCellId(null);
        setEditingCellId(null);
        setEditValue("");
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [editValue]
  );

  const applyOverrideToCache = useCallback(
    (
      cellId: string,
      _fieldId: string,
      patch: { corrected_value: unknown; corrected_at: string | null }
    ) => {
      qc.setQueryData<CellsResponse | { empty: true }>(
        ["cells", projectId],
        (prev) => {
          if (!prev || "empty" in prev) return prev;
          return {
            ...prev,
            rows: prev.rows.map((r) => ({
              ...r,
              cells: r.cells.map((c) =>
                c.id === cellId
                  ? {
                      ...c,
                      corrected_value: patch.corrected_value,
                      corrected_at: patch.corrected_at,
                    }
                  : c
              ),
            })),
          };
        }
      );
    },
    [projectId, qc]
  );

  // ---- Build columns ----
  type RowRecord = ApiRow & { _index: number };

  const tableData = useMemo<RowRecord[]>(
    () => filteredRows.map((r, i) => ({ ...r, _index: i })),
    [filteredRows]
  );

  const columns = useMemo<ColumnDef<RowRecord>[]>(() => {
    const fixed: ColumnDef<RowRecord>[] = [
      {
        id: "_row",
        header: () => <span className="text-[var(--subtle)]">#</span>,
        size: 48,
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-[var(--subtle)] text-xs tabular-nums">
            {row.original._index + 1}
          </span>
        ),
      },
      {
        id: "_thumb",
        header: () => <span className="sr-only">Thumbnail</span>,
        size: 64,
        enableSorting: false,
        cell: ({ row }) =>
          row.original.signed_url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={row.original.signed_url}
              alt={row.original.filename}
              onClick={() => setZoomSrc(row.original.signed_url!)}
              className="w-12 h-12 object-cover rounded-md border border-[var(--border)] cursor-zoom-in hover:ring-2 hover:ring-[var(--primary)]/40 transition-all"
            />
          ) : (
            <div className="w-12 h-12 rounded-md border border-[var(--border)] bg-[var(--surface-muted)]" />
          ),
      },
      {
        id: "filename",
        accessorFn: (row) => row.filename,
        header: "Filename",
        size: 240,
        enableSorting: true,
        cell: ({ row }) => (
          <span className="text-sm text-[var(--foreground)] truncate block max-w-[220px]">
            {row.original.filename}
          </span>
        ),
        sortingFn: (a, b) => a.original.filename.localeCompare(b.original.filename),
      },
      {
        id: "_status",
        header: () => <span className="sr-only">Row status</span>,
        size: 32,
        enableSorting: false,
        cell: ({ row }) => {
          const worst = row.original.cells.reduce<CellStatus>(
            (acc, c) =>
              STATUS_RANK[c.status] > STATUS_RANK[acc] ? c.status : acc,
            "ok"
          );
          if (worst === "ok") return null;
          if (worst === "failed")
            return <X size={14} className="text-red-500" strokeWidth={3} />;
          if (worst === "needs_review")
            return <AlertTriangle size={14} className="text-amber-500" />;
          return <span className="w-2 h-2 rounded-full bg-amber-400 inline-block" />;
        },
      },
    ];

    const dyn: ColumnDef<RowRecord>[] = fields.map((f) => ({
      id: `field_${f.id}`,
      accessorFn: (row) => {
        const c = row.cells.find((x) => x.field_id === f.id);
        return sortValue(c);
      },
      header: () => {
        const issues = issueCountByField.get(f.id) ?? 0;
        return (
          <div className="flex items-center gap-2">
            <span className="font-mono normal-case tracking-normal text-[var(--foreground)]">{f.field_name}</span>
            {issues > 0 && (
              <Badge tone="red" className="!px-1.5 normal-case tracking-normal">
                {issues}
              </Badge>
            )}
          </div>
        );
      },
      enableSorting: true,
      size: 220,
      cell: ({ row }) => {
        const cell = row.original.cells.find((x) => x.field_id === f.id);
        if (!cell) return <span className="text-zinc-300 text-sm">—</span>;
        return (
          <ValueCell
            cell={cell}
            isEditing={editingCellId === cell.id}
            saving={savingCellId === cell.id}
            editValue={editValue}
            onEditValueChange={setEditValue}
            onBeginEdit={() => beginEdit(cell)}
            onCancel={cancelEdit}
            onCommit={() => commitEdit(cell, f.id)}
            onZoom={setZoomSrc}
          />
        );
      },
    }));

    return [...fixed, ...dyn];
  }, [
    fields,
    issueCountByField,
    editingCellId,
    savingCellId,
    editValue,
    beginEdit,
    cancelEdit,
    commitEdit,
  ]);

  // TanStack Table v8 returns non-memoizable functions; React Compiler skips
  // compiling this component as a result. Suppression is intentional — the
  // hook is used in isolation here, its return value isn't passed to other
  // memoized components, so the staleness risk doesn't apply.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable<RowRecord>({
    data: tableData,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  // Virtualizer
  const parentRef = useRef<HTMLDivElement>(null);
  const tableRows = table.getRowModel().rows;
  const virtualizer = useVirtualizer({
    count: tableRows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 8,
  });

  const crumbs = [
    { label: "Project", href: `/projects/${projectId}` },
    { label: "Review" },
  ];

  // ---- Render ----
  if (cellsQuery.isLoading) {
    return (
      <Shell crumbs={crumbs} fullBleed>
        <div className="flex items-center justify-center py-24 text-[var(--muted)] text-sm">
          <Loader2 size={16} className="animate-spin mr-2" />
          Loading review table…
        </div>
      </Shell>
    );
  }

  if (cellsQuery.isError) {
    return (
      <Shell crumbs={crumbs} fullBleed>
        <div className="text-center py-24 text-red-600 text-sm">
          {cellsQuery.error instanceof Error
            ? cellsQuery.error.message
            : "Failed to load"}
        </div>
      </Shell>
    );
  }

  if (isEmpty || !cellsData) {
    return (
      <Shell crumbs={crumbs} fullBleed>
        <div className="max-w-md mx-auto py-24 text-center space-y-4">
          <p className="text-[var(--muted)] text-sm">
            No completed job yet — process your images first.
          </p>
          <Link
            href={`/projects/${projectId}`}
            className="inline-block text-sm font-medium text-[var(--primary)] hover:underline"
          >
            ← Back to project
          </Link>
        </div>
      </Shell>
    );
  }

  return (
    <Shell
      crumbs={crumbs}
      fullBleed
      contentClassName="flex flex-col min-h-0"
      rightSlot={
        <>
          <Button
            size="sm"
            variant={issuesOnly ? "primary" : "secondary"}
            onClick={() => setIssuesOnly((v) => !v)}
          >
            Issues only
            {totalIssueCount > 0 && (
              <Badge tone={issuesOnly ? "neutral" : "red"} className="!py-0">
                {totalIssueCount}
              </Badge>
            )}
          </Button>
          <Link
            href={`/projects/${projectId}/export`}
            className="inline-flex items-center gap-2 h-8 px-3 text-xs font-medium rounded-lg bg-[var(--surface)] text-[var(--foreground)] border border-[var(--border-strong)] hover:bg-[var(--surface-muted)] shadow-sm transition-colors"
          >
            <Download size={13} /> Export CSV
          </Link>
        </>
      }
    >
      <div className="px-6 pt-5 pb-3 flex items-center gap-3 border-b border-[var(--border)] bg-[var(--surface)]">
        <h2 className="text-base font-semibold text-[var(--foreground)]">Extracted values</h2>
        <span className="text-xs text-[var(--subtle)]">
          {rows.length} {rows.length === 1 ? "image" : "images"} · {fields.length}{" "}
          {fields.length === 1 ? "field" : "fields"}
        </span>
        <span className="ml-auto text-[10px] text-[var(--subtle)]">
          Double-click a cell to edit · <kbd className="px-1 py-0.5 rounded border border-[var(--border)] bg-[var(--surface-muted)] font-mono">Esc</kbd> to cancel
        </span>
      </div>

      <div
        ref={parentRef}
        className="flex-1 overflow-auto bg-[var(--surface)]"
        style={{ contain: "strict" }}
      >
        {/* Flex-based layout: explicit width on every th/td keeps header and
            virtual rows in sync without colgroup (which can't go inside tr). */}
        <table style={{ borderCollapse: "collapse", width: "max-content", minWidth: "100%" }} className="text-sm">
          <thead style={{ display: "block", position: "sticky", top: 0, zIndex: 10, background: "var(--surface-muted)" }}>
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id} style={{ display: "flex" }} className="border-b border-[var(--border)]">
                {hg.headers.map((header) => {
                  const canSort = header.column.getCanSort();
                  const sortDir = header.column.getIsSorted();
                  return (
                    <th
                      key={header.id}
                      style={{ width: `${header.getSize()}px`, flexShrink: 0 }}
                      className="text-left text-[11px] uppercase tracking-wide font-semibold text-[var(--muted)] px-3 py-2.5 overflow-hidden"
                    >
                      <div
                        className={`flex items-center gap-1.5 ${
                          canSort ? "cursor-pointer select-none hover:text-[var(--foreground)]" : ""
                        }`}
                        onClick={
                          canSort
                            ? header.column.getToggleSortingHandler()
                            : undefined
                        }
                      >
                        {header.isPlaceholder
                          ? null
                          : flexRender(
                              header.column.columnDef.header,
                              header.getContext()
                            )}
                        {canSort &&
                          (sortDir === "asc" ? (
                            <ArrowUp size={12} className="text-[var(--primary)]" />
                          ) : sortDir === "desc" ? (
                            <ArrowDown size={12} className="text-[var(--primary)]" />
                          ) : (
                            <ArrowUpDown size={12} className="text-[var(--subtle)]" />
                          ))}
                      </div>
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody
            style={{
              display: "block",
              height: `${virtualizer.getTotalSize()}px`,
              position: "relative",
            }}
          >
            {virtualizer.getVirtualItems().map((vRow) => {
              const row = tableRows[vRow.index];
              return (
                <tr
                  key={row.id}
                  data-index={vRow.index}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    height: `${vRow.size}px`,
                    transform: `translateY(${vRow.start}px)`,
                    display: "flex",
                  }}
                  className="border-b border-[var(--border)] hover:bg-[var(--surface-muted)]/60"
                >
                  {row.getVisibleCells().map((cell) => (
                    <td
                      key={cell.id}
                      style={{ width: `${cell.column.getSize()}px`, flexShrink: 0 }}
                      className="px-3 py-2 flex items-center overflow-hidden"
                    >
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext()
                      )}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {zoomSrc && (
        <div
          className="fixed inset-0 bg-black/80 z-50 flex items-center justify-center"
          onClick={() => setZoomSrc(null)}
        >
          <div className="relative max-w-3xl max-h-[90vh] p-2" onClick={(e) => e.stopPropagation()}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={zoomSrc} alt="Crop zoom" className="max-w-full max-h-[85vh] object-contain rounded shadow-2xl" />
            <button
              onClick={() => setZoomSrc(null)}
              className="absolute top-3 right-3 text-white bg-black/50 rounded-full p-1 hover:bg-black/80"
            >
              <X size={18} />
            </button>
          </div>
        </div>
      )}
    </Shell>
  );
}

// ---------------- ValueCell ----------------

type ValueCellProps = {
  cell: ApiCell;
  isEditing: boolean;
  saving: boolean;
  editValue: string;
  onEditValueChange: (v: string) => void;
  onBeginEdit: () => void;
  onCancel: () => void;
  onCommit: () => void;
  onZoom: (src: string) => void;
};

function ValueCell({
  cell,
  isEditing,
  saving,
  editValue,
  onEditValueChange,
  onBeginEdit,
  onCancel,
  onCommit,
  onZoom,
}: ValueCellProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);

  const isCorrected =
    cell.corrected_value !== null && cell.corrected_value !== undefined;
  const text = displayValue(cell);

  if (isEditing) {
    return (
      <div className="flex items-center gap-2">
        <input
          ref={inputRef}
          type="text"
          value={editValue}
          disabled={saving}
          onChange={(e) => onEditValueChange(e.target.value)}
          onBlur={onCommit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              onCommit();
            } else if (e.key === "Escape") {
              e.preventDefault();
              onCancel();
            }
          }}
          className="flex-1 min-w-0 text-sm border border-[var(--primary)] rounded-md px-2 py-1 focus:outline-none bg-[var(--surface)]"
        />
        {saving && <Loader2 size={14} className="animate-spin text-[var(--subtle)]" />}
      </div>
    );
  }

  // Failed cell — grey italic, no crop, but still editable
  if (cell.status === "failed") {
    return (
      <div
        tabIndex={0}
        onDoubleClick={onBeginEdit}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); onBeginEdit(); }
        }}
        className="group flex items-center gap-2 cursor-text outline-none focus:ring-1 focus:ring-[var(--primary)]/40 rounded px-1"
      >
        <span className="text-[var(--subtle)] italic text-sm">Not found</span>
        <button
          onClick={(e) => { e.stopPropagation(); onBeginEdit(); }}
          className="opacity-0 group-hover:opacity-100 flex-shrink-0 p-0.5 rounded hover:bg-[var(--surface-muted)] transition-opacity"
          title="Enter value manually"
        >
          <Pencil size={11} className="text-[var(--subtle)]" />
        </button>
      </div>
    );
  }

  const dot =
    cell.status === "ok" ? (
      <span className="w-2 h-2 rounded-full bg-emerald-500 inline-block flex-shrink-0" />
    ) : cell.status === "low_confidence" ? (
      <span className="w-2 h-2 rounded-full bg-amber-400 inline-block flex-shrink-0" />
    ) : (
      <AlertTriangle size={12} className="text-amber-500 flex-shrink-0" />
    );

  const suffix =
    cell.status === "low_confidence" && cell.combined_confidence != null ? (
      <span className="text-[10px] text-[var(--subtle)] tabular-nums">
        {Math.round(cell.combined_confidence * 100)}%
      </span>
    ) : null;

  const showText =
    cell.status === "needs_review" && cell.validation_error
      ? cell.validation_error
      : text;

  return (
    <div
      tabIndex={0}
      onDoubleClick={onBeginEdit}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          onBeginEdit();
        }
      }}
      className="group flex items-center gap-2 cursor-text outline-none focus:ring-1 focus:ring-[var(--primary)]/40 rounded px-1 py-0.5"
      title={
        isCorrected
          ? `Original: ${
              cell.parsed_value != null
                ? String(cell.parsed_value)
                : cell.raw_text ?? ""
            }`
          : undefined
      }
    >
      {cell.crop_signed_url && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={cell.crop_signed_url}
          alt=""
          onClick={(e) => { e.stopPropagation(); onZoom(cell.crop_signed_url!); }}
          className="w-12 h-12 object-cover rounded-md border border-[var(--border)] cursor-zoom-in transition-transform group-hover:scale-[1.6] group-hover:z-10 group-hover:shadow-lg"
        />
      )}
      <div className="flex items-center gap-1.5 min-w-0 flex-1">
        {isCorrected ? (
          <Pencil size={12} className="text-[var(--primary)] flex-shrink-0" />
        ) : (
          dot
        )}
        <span
          className={`text-sm truncate ${
            isCorrected
              ? "text-[var(--primary)] font-medium"
              : cell.status === "needs_review"
              ? "text-amber-700 dark:text-amber-300"
              : "text-[var(--foreground)]"
          }`}
        >
          {showText || <span className="text-[var(--subtle)]">—</span>}
        </span>
        {suffix}
        <button
          onClick={(e) => { e.stopPropagation(); onBeginEdit(); }}
          className="ml-auto opacity-0 group-hover:opacity-100 flex-shrink-0 p-0.5 rounded hover:bg-[var(--surface-muted)] transition-opacity"
          title="Edit value"
        >
          <Pencil size={11} className="text-[var(--subtle)]" />
        </button>
      </div>
    </div>
  );
}
