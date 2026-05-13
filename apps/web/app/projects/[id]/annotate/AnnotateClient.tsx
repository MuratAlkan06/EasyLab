"use client";

import {
  startTransition,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Stage, Layer, Image as KImage, Rect, Transformer } from "react-konva";
import type Konva from "konva";
import type { KonvaEventObject } from "konva/lib/Node";
import { toast } from "sonner";
import { Pencil, MousePointer2, Trash2, Loader2 } from "lucide-react";

const COLOR_PALETTE = [
  "#EF4444",
  "#F97316",
  "#EAB308",
  "#22C55E",
  "#3B82F6",
  "#8B5CF6",
  "#EC4899",
  "#14B8A6",
];

const MAX_FIELDS = 20;
const MIN_BOX_PX = 20;
const SIDEBAR_WIDTH = 320;

type ExpectedFormat = {
  type: "number" | "text" | "boolean";
  unit?: string;
};

type FieldState = {
  id: string;
  field_name: string;
  x: number; // normalized 0..1
  y: number;
  width: number;
  height: number;
  color: string;
  expected_format?: ExpectedFormat;
};

type ApiImage = {
  id: string;
  filename: string;
  status: string;
  is_reference: boolean;
  width_px: number | null;
  height_px: number | null;
  signed_url: string | null;
};

type ApiField = {
  id: string;
  field_name: string;
  display_order: number;
  reference_box: { x: number; y: number; width: number; height: number };
  semantic_description: string | null;
  expected_format: ExpectedFormat | null;
};

type Mode = "draw" | "select";

export default function AnnotateClient({ projectId }: { projectId: string }) {
  const router = useRouter();
  const containerRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<Konva.Stage>(null);
  const transformerRef = useRef<Konva.Transformer>(null);
  const rectRefs = useRef<Map<string, Konva.Rect>>(new Map());
  const [focusFieldId, setFocusFieldId] = useState<string | null>(null);

  const [mode, setMode] = useState<Mode>("draw");
  const [fields, setFields] = useState<FieldState[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [image, setImage] = useState<HTMLImageElement | null>(null);
  const [stageSize, setStageSize] = useState({ width: 0, height: 0 });
  const [drawStart, setDrawStart] = useState<{ x: number; y: number } | null>(
    null
  );
  const [drawCurrent, setDrawCurrent] = useState<{
    x: number;
    y: number;
  } | null>(null);
  const [saving, setSaving] = useState(false);

  // 1. Fetch reference image
  const imagesQuery = useQuery({
    queryKey: ["images", projectId, "uploaded"],
    queryFn: async () => {
      const res = await fetch(
        `/api/projects/${projectId}/images?status=uploaded`
      );
      if (!res.ok) throw new Error("Failed to load images");
      return (await res.json()) as { images: ApiImage[] };
    },
  });

  // 2. Fetch existing fields
  const fieldsQuery = useQuery({
    queryKey: ["fields", projectId],
    queryFn: async () => {
      const res = await fetch(`/api/projects/${projectId}/fields`);
      if (!res.ok) throw new Error("Failed to load fields");
      return (await res.json()) as { fields: ApiField[] };
    },
  });

  const referenceImage = useMemo(
    () => imagesQuery.data?.images.find((i) => i.is_reference) ?? null,
    [imagesQuery.data]
  );

  // Redirect if no reference
  useEffect(() => {
    if (imagesQuery.isSuccess && !referenceImage) {
      router.replace(`/projects/${projectId}/upload`);
    }
  }, [imagesQuery.isSuccess, referenceImage, projectId, router]);

  // Load reference image as HTMLImageElement
  useEffect(() => {
    if (!referenceImage?.signed_url) return;
    const img = new window.Image();
    img.crossOrigin = "anonymous";
    img.onload = () => setImage(img);
    img.onerror = () => toast.error("Failed to load reference image");
    img.src = referenceImage.signed_url;
  }, [referenceImage?.signed_url]);

  // Hydrate fields from server
  useEffect(() => {
    if (!fieldsQuery.data) return;
    if (fields.length > 0) return; // only initial hydration
    const hydrated: FieldState[] = fieldsQuery.data.fields.map((f, idx) => ({
      id: crypto.randomUUID(),
      field_name: f.field_name,
      x: f.reference_box.x,
      y: f.reference_box.y,
      width: f.reference_box.width,
      height: f.reference_box.height,
      color: COLOR_PALETTE[idx % COLOR_PALETTE.length],
      expected_format: f.expected_format ?? undefined,
    }));
    if (hydrated.length > 0) startTransition(() => setFields(hydrated));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fieldsQuery.data]);

  // Resize observer for stage
  useEffect(() => {
    if (!containerRef.current || !image) return;
    const el = containerRef.current;
    const update = () => {
      const w = el.clientWidth;
      const h = el.clientHeight;
      const aspect = image.width / image.height;
      // Fit image entirely inside container
      let dispW = w;
      let dispH = w / aspect;
      if (dispH > h) {
        dispH = h;
        dispW = h * aspect;
      }
      setStageSize({ width: Math.floor(dispW), height: Math.floor(dispH) });
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [image]);

  // Attach Transformer to selected node (select mode only)
  useEffect(() => {
    const tr = transformerRef.current;
    if (!tr) return;
    if (mode !== "select" || !selectedId) {
      tr.nodes([]);
      tr.getLayer()?.batchDraw();
      return;
    }
    const node = rectRefs.current.get(selectedId);
    if (node) {
      tr.nodes([node]);
      tr.getLayer()?.batchDraw();
    }
  }, [mode, selectedId, fields]);

  // Keyboard: 'S' toggle, 'D' draw, Delete removes selected
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT")) {
        return;
      }
      if (e.key === "s" || e.key === "S") {
        setMode("select");
      } else if (e.key === "d" || e.key === "D") {
        setMode("draw");
        setSelectedId(null);
      } else if ((e.key === "Delete" || e.key === "Backspace") && selectedId) {
        setFields((prev) => prev.filter((f) => f.id !== selectedId));
        setSelectedId(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId]);

  // ---- Drawing handlers ----
  const onStageMouseDown = useCallback(
    (e: KonvaEventObject<MouseEvent>) => {
      if (mode !== "draw") {
        // In select mode, clicking empty stage clears selection
        if (e.target === e.target.getStage()) {
          setSelectedId(null);
        }
        return;
      }
      if (fields.length >= MAX_FIELDS) {
        toast.error(`Maximum ${MAX_FIELDS} fields reached`);
        return;
      }
      const stage = stageRef.current;
      const pos = stage?.getPointerPosition();
      if (!pos) return;
      setDrawStart({ x: pos.x, y: pos.y });
      setDrawCurrent({ x: pos.x, y: pos.y });
    },
    [mode, fields.length]
  );

  const onStageMouseMove = useCallback(() => {
    if (mode !== "draw" || !drawStart) return;
    const pos = stageRef.current?.getPointerPosition();
    if (!pos) return;
    setDrawCurrent({ x: pos.x, y: pos.y });
  }, [mode, drawStart]);

  const onStageMouseUp = useCallback(() => {
    if (mode !== "draw" || !drawStart || !drawCurrent) {
      setDrawStart(null);
      setDrawCurrent(null);
      return;
    }
    const x1 = Math.min(drawStart.x, drawCurrent.x);
    const y1 = Math.min(drawStart.y, drawCurrent.y);
    const x2 = Math.max(drawStart.x, drawCurrent.x);
    const y2 = Math.max(drawStart.y, drawCurrent.y);
    const wPx = x2 - x1;
    const hPx = y2 - y1;
    setDrawStart(null);
    setDrawCurrent(null);
    if (wPx < MIN_BOX_PX || hPx < MIN_BOX_PX) return;
    if (stageSize.width === 0 || stageSize.height === 0) return;

    // Clamp to stage bounds
    const cx1 = Math.max(0, Math.min(x1, stageSize.width));
    const cy1 = Math.max(0, Math.min(y1, stageSize.height));
    const cx2 = Math.max(0, Math.min(x2, stageSize.width));
    const cy2 = Math.max(0, Math.min(y2, stageSize.height));

    const nx = cx1 / stageSize.width;
    const ny = cy1 / stageSize.height;
    const nw = (cx2 - cx1) / stageSize.width;
    const nh = (cy2 - cy1) / stageSize.height;

    const id = crypto.randomUUID();
    const newField: FieldState = {
      id,
      field_name: "",
      x: nx,
      y: ny,
      width: nw,
      height: nh,
      color: COLOR_PALETTE[fields.length % COLOR_PALETTE.length],
    };
    setFocusFieldId(id);
    setFields((prev) => [...prev, newField]);
  }, [mode, drawStart, drawCurrent, stageSize, fields.length]);

  // ---- Field updates ----
  const updateField = useCallback(
    (id: string, patch: Partial<FieldState>) => {
      setFields((prev) =>
        prev.map((f) => (f.id === id ? { ...f, ...patch } : f))
      );
    },
    []
  );

  const deleteField = useCallback(
    (id: string) => {
      rectRefs.current.delete(id);
      setFields((prev) => prev.filter((f) => f.id !== id));
      if (selectedId === id) setSelectedId(null);
    },
    [selectedId]
  );

  // ---- Save ----
  const onSave = useCallback(async () => {
    if (fields.length === 0) {
      toast.error("Draw at least one field");
      return;
    }
    const trimmed = fields.map((f) => ({ ...f, field_name: f.field_name.trim() }));
    if (trimmed.some((f) => !f.field_name)) {
      toast.error("All fields must have a name");
      return;
    }
    const names = trimmed.map((f) => f.field_name.toLowerCase());
    if (new Set(names).size !== names.length) {
      toast.error("Field names must be unique");
      return;
    }
    setSaving(true);
    try {
      const payload = {
        fields: trimmed.map((f) => ({
          field_name: f.field_name,
          reference_box: {
            x: f.x,
            y: f.y,
            width: f.width,
            height: f.height,
          },
          ...(f.expected_format
            ? {
                expected_format: {
                  type: f.expected_format.type,
                  ...(f.expected_format.unit
                    ? { unit: f.expected_format.unit }
                    : {}),
                },
              }
            : {}),
        })),
      };
      const res = await fetch(`/api/projects/${projectId}/fields`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error?.message ?? "Failed to save fields");
      }
      router.push(`/projects/${projectId}/process`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to save fields");
    } finally {
      setSaving(false);
    }
  }, [fields, projectId, router]);

  // Loading / error states
  if (imagesQuery.isLoading || fieldsQuery.isLoading) {
    return (
      <div className="flex items-center justify-center h-screen text-zinc-500 text-sm">
        Loading…
      </div>
    );
  }
  if (imagesQuery.isError) {
    return (
      <div className="flex items-center justify-center h-screen text-red-600 text-sm">
        Failed to load images
      </div>
    );
  }
  if (!referenceImage) {
    // redirect in flight
    return null;
  }

  // Preview rect coords during draw
  const previewRect =
    drawStart && drawCurrent
      ? {
          x: Math.min(drawStart.x, drawCurrent.x),
          y: Math.min(drawStart.y, drawCurrent.y),
          width: Math.abs(drawCurrent.x - drawStart.x),
          height: Math.abs(drawCurrent.y - drawStart.y),
        }
      : null;

  return (
    <div className="flex h-screen bg-[var(--surface-muted)]">
      {/* Canvas area */}
      <div className="flex-1 flex flex-col min-w-0">
        <Toolbar mode={mode} onChange={setMode} count={fields.length} />
        <div
          ref={containerRef}
          className="flex-1 flex items-center justify-center overflow-hidden p-6"
          style={{ cursor: mode === "draw" ? "crosshair" : "default" }}
        >
          {image && stageSize.width > 0 && (
            <Stage
              ref={stageRef}
              width={stageSize.width}
              height={stageSize.height}
              onMouseDown={onStageMouseDown}
              onMouseMove={onStageMouseMove}
              onMouseUp={onStageMouseUp}
              className="bg-white shadow-lg shadow-slate-900/10 rounded-md overflow-hidden"
            >
              <Layer listening={false}>
                <KImage
                  image={image}
                  width={stageSize.width}
                  height={stageSize.height}
                />
              </Layer>
              <Layer>
                {fields.map((f) => (
                  <Rect
                    key={f.id}
                    ref={(node) => {
                      if (node) rectRefs.current.set(f.id, node);
                      else rectRefs.current.delete(f.id);
                    }}
                    x={f.x * stageSize.width}
                    y={f.y * stageSize.height}
                    width={f.width * stageSize.width}
                    height={f.height * stageSize.height}
                    stroke={f.color}
                    strokeWidth={2}
                    fill={`${f.color}22`}
                    draggable={mode === "select"}
                    listening={mode === "select"}
                    onClick={() => {
                      if (mode === "select") setSelectedId(f.id);
                    }}
                    onTap={() => {
                      if (mode === "select") setSelectedId(f.id);
                    }}
                    onDragEnd={(e) => {
                      const node = e.target;
                      const nx = node.x() / stageSize.width;
                      const ny = node.y() / stageSize.height;
                      updateField(f.id, {
                        x: Math.max(0, Math.min(1 - f.width, nx)),
                        y: Math.max(0, Math.min(1 - f.height, ny)),
                      });
                    }}
                    onTransformEnd={(e) => {
                      const node = e.target as Konva.Rect;
                      const scaleX = node.scaleX();
                      const scaleY = node.scaleY();
                      const newWPx = Math.max(MIN_BOX_PX, node.width() * scaleX);
                      const newHPx = Math.max(MIN_BOX_PX, node.height() * scaleY);
                      node.scaleX(1);
                      node.scaleY(1);
                      node.width(newWPx);
                      node.height(newHPx);
                      const nx = node.x() / stageSize.width;
                      const ny = node.y() / stageSize.height;
                      const nw = newWPx / stageSize.width;
                      const nh = newHPx / stageSize.height;
                      updateField(f.id, {
                        x: Math.max(0, Math.min(1, nx)),
                        y: Math.max(0, Math.min(1, ny)),
                        width: Math.max(0, Math.min(1 - nx, nw)),
                        height: Math.max(0, Math.min(1 - ny, nh)),
                      });
                    }}
                  />
                ))}
                {previewRect && (
                  <Rect
                    x={previewRect.x}
                    y={previewRect.y}
                    width={previewRect.width}
                    height={previewRect.height}
                    stroke="#3B82F6"
                    strokeWidth={2}
                    dash={[4, 4]}
                    fill="#3B82F622"
                    listening={false}
                  />
                )}
                {mode === "select" && (
                  <Transformer
                    ref={transformerRef}
                    rotateEnabled={false}
                    boundBoxFunc={(oldBox, newBox) => {
                      if (newBox.width < MIN_BOX_PX || newBox.height < MIN_BOX_PX) {
                        return oldBox;
                      }
                      return newBox;
                    }}
                  />
                )}
              </Layer>
            </Stage>
          )}
        </div>
      </div>

      {/* Sidebar */}
      <FieldSidebar
        fields={fields}
        selectedId={selectedId}
        focusFieldId={focusFieldId}
        onClearFocus={() => setFocusFieldId(null)}
        onSelect={(id) => {
          setMode("select");
          setSelectedId(id);
        }}
        onUpdate={updateField}
        onDelete={deleteField}
        onSave={onSave}
        saving={saving}
      />
    </div>
  );
}

// ----------------- Toolbar -----------------

function Toolbar({
  mode,
  onChange,
  count,
}: {
  mode: Mode;
  onChange: (m: Mode) => void;
  count: number;
}) {
  const btn = (active: boolean) =>
    `flex items-center gap-1.5 px-3 h-8 rounded-md text-sm font-medium border transition-colors ${
      active
        ? "bg-gradient-to-b from-indigo-500 to-indigo-600 text-white border-indigo-600 shadow-sm shadow-indigo-500/30"
        : "bg-[var(--surface)] text-[var(--foreground)] border-[var(--border-strong)] hover:bg-[var(--surface-muted)]"
    }`;
  return (
    <div className="flex items-center gap-2 px-4 h-12 bg-[var(--surface)] border-b border-[var(--border)]">
      <button className={btn(mode === "draw")} onClick={() => onChange("draw")}>
        <Pencil size={14} /> Draw
      </button>
      <button className={btn(mode === "select")} onClick={() => onChange("select")}>
        <MousePointer2 size={14} /> Select
      </button>
      <span className="text-[10px] text-[var(--subtle)] ml-1">
        Press <kbd className="px-1 py-0.5 rounded border border-[var(--border)] bg-[var(--surface-muted)] font-mono">D</kbd> /{" "}
        <kbd className="px-1 py-0.5 rounded border border-[var(--border)] bg-[var(--surface-muted)] font-mono">S</kbd> to switch
      </span>
      <span className="ml-auto text-xs font-medium text-[var(--muted)] tabular-nums">
        {count} / {MAX_FIELDS} fields
      </span>
    </div>
  );
}

// ----------------- Sidebar -----------------

type SidebarProps = {
  fields: FieldState[];
  selectedId: string | null;
  focusFieldId: string | null;
  onClearFocus: () => void;
  onSelect: (id: string) => void;
  onUpdate: (id: string, patch: Partial<FieldState>) => void;
  onDelete: (id: string) => void;
  onSave: () => void;
  saving: boolean;
};

function FieldSidebar({
  fields,
  selectedId,
  focusFieldId,
  onClearFocus,
  onSelect,
  onUpdate,
  onDelete,
  onSave,
  saving,
}: SidebarProps) {
  return (
    <aside
      className="flex flex-col bg-[var(--surface)] border-l border-[var(--border)]"
      style={{ width: SIDEBAR_WIDTH }}
    >
      <div className="px-4 py-3.5 border-b border-[var(--border)]">
        <h2 className="text-sm font-semibold text-[var(--foreground)]">Fields</h2>
        <p className="text-xs text-[var(--muted)] mt-0.5">
          Draw boxes on the reference image, then name each one.
        </p>
      </div>
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {fields.length === 0 ? (
          <div className="text-center py-12 px-4">
            <div className="text-xs text-[var(--subtle)]">No fields yet.</div>
            <div className="text-xs text-[var(--muted)] mt-1">
              Drag a rectangle on the image to start.
            </div>
          </div>
        ) : (
          fields.map((f) => (
            <FieldRow
              key={f.id}
              field={f}
              selected={selectedId === f.id}
              autoFocus={focusFieldId === f.id}
              onAutoFocused={onClearFocus}
              onSelect={() => onSelect(f.id)}
              onChange={(patch) => onUpdate(f.id, patch)}
              onDelete={() => onDelete(f.id)}
            />
          ))
        )}
      </div>
      <div className="p-3 border-t border-[var(--border)] bg-[var(--surface-muted)]/50">
        <button
          className="w-full flex items-center justify-center gap-2 h-10 rounded-lg bg-gradient-to-b from-indigo-500 to-indigo-600 text-white text-sm font-medium shadow-sm shadow-indigo-500/30 hover:from-indigo-500 hover:to-indigo-700 active:translate-y-px disabled:opacity-50 disabled:cursor-not-allowed transition-all"
          disabled={saving || fields.length === 0}
          onClick={onSave}
        >
          {saving && <Loader2 size={14} className="animate-spin" />}
          {saving ? "Saving…" : "Save & continue"}
        </button>
      </div>
    </aside>
  );
}

// ----------------- FieldRow -----------------

function FieldRow({
  field,
  selected,
  autoFocus,
  onAutoFocused,
  onSelect,
  onChange,
  onDelete,
}: {
  field: FieldState;
  selected: boolean;
  autoFocus: boolean;
  onAutoFocused: () => void;
  onSelect: () => void;
  onChange: (patch: Partial<FieldState>) => void;
  onDelete: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (autoFocus && inputRef.current) {
      inputRef.current.focus();
      onAutoFocused();
    }
  }, [autoFocus, onAutoFocused]);

  const fmt = field.expected_format;
  return (
    <div
      onClick={onSelect}
      className={`rounded-lg border p-2.5 space-y-2 cursor-pointer transition-colors ${
        selected
          ? "border-[var(--primary)] bg-[var(--primary-soft)] shadow-sm shadow-indigo-500/10"
          : "border-[var(--border)] bg-[var(--surface)] hover:border-[var(--border-strong)]"
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className="inline-block w-3 h-3 rounded-full flex-shrink-0 ring-2 ring-white shadow-sm"
          style={{ backgroundColor: field.color }}
        />
        <input
          ref={inputRef}
          className="flex-1 min-w-0 h-8 text-sm border border-[var(--border-strong)] bg-[var(--surface)] rounded-md px-2 placeholder:text-[var(--subtle)] focus:outline-none focus:border-[var(--primary)]"
          placeholder="field_name"
          value={field.field_name}
          maxLength={100}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => onChange({ field_name: e.target.value })}
        />
        <button
          className="p-1.5 rounded-md text-[var(--subtle)] hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-500/10 transition-colors"
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          aria-label="Delete field"
        >
          <Trash2 size={13} />
        </button>
      </div>
      <div className="flex items-center gap-2">
        <select
          className="h-7 text-xs border border-[var(--border)] rounded-md px-1.5 bg-[var(--surface)] text-[var(--muted)]"
          value={fmt?.type ?? ""}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => {
            const v = e.target.value;
            if (!v) {
              onChange({ expected_format: undefined });
            } else {
              onChange({
                expected_format: {
                  type: v as ExpectedFormat["type"],
                  unit: fmt?.unit,
                },
              });
            }
          }}
        >
          <option value="">format…</option>
          <option value="number">number</option>
          <option value="text">text</option>
          <option value="boolean">boolean</option>
        </select>
        {fmt?.type === "number" && (
          <input
            className="flex-1 min-w-0 h-7 text-xs border border-[var(--border)] bg-[var(--surface)] rounded-md px-2 placeholder:text-[var(--subtle)] focus:outline-none focus:border-[var(--primary)]"
            placeholder="unit (e.g. mV)"
            value={fmt.unit ?? ""}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) =>
              onChange({
                expected_format: { type: "number", unit: e.target.value },
              })
            }
          />
        )}
      </div>
    </div>
  );
}
