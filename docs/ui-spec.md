# EasyLab — UI/UX Specification (v1 MVP)

> Generated: 2026-05-05
> Framework: Next.js 15 App Router. All routes are under `apps/web/app/`.
> Minimum viewport: 1280px wide (desktop only — no mobile layout in v1).

---

## Table of Contents

1. [Route Map](#routes)
2. [Page Specs](#pages)
3. [Annotation Canvas](#canvas)
4. [Review Table](#review-table)
5. [Processing Page](#processing)
6. [Component Hierarchy](#components)
7. [Toast Strategy](#toasts)

---

## 1. Route Map {#routes}

| Route | Page | Entry condition |
|---|---|---|
| `/` | Dashboard | Always accessible |
| `/projects/[id]` | Project hub (stepper) | Project exists in workspace |
| `/projects/[id]/upload` | Upload + reference selection | Project status = draft |
| `/projects/[id]/annotate` | Annotation canvas | ≥1 image uploaded, reference image selected |
| `/projects/[id]/process` | Job progress | Project status = annotated, ≥1 field saved |
| `/projects/[id]/review` | Review table | Latest job status = succeeded (or partial) |
| `/projects/[id]/export` | CSV export | Same as review |

Navigating to a locked step redirects to the correct current step.

---

## 2. Page Specs {#pages}

### `/` — Dashboard

**Purpose:** List all projects in the workspace. Create new projects.

**States:**
- Empty: "No projects yet" illustration + "New Project" button
- Populated: table with columns (name, image count, status, last updated) + "New Project" button
- Creating: inline name input with confirm/cancel (no modal)

**Actions:**
- "New Project" → POST /api/projects → navigate to `/projects/[id]/upload`
- Click row → navigate to `/projects/[id]` (stepper hub)
- Delete project → DELETE /api/projects/:id (confirmation dialog)

---

### `/projects/[id]` — Project Hub

**Purpose:** Stepper navigation showing which steps are complete.

**Stepper steps:** Upload → Annotate → Process → Review → Export

**Rules:**
- Completed steps show a checkmark and are clickable
- Current step is highlighted
- Future locked steps are greyed out and not clickable
- Processing started = Upload and Annotate steps are locked (cannot go back)

---

### `/projects/[id]/upload` — Upload + Reference Selection

**Purpose:** Upload batch images and select one as the reference.

**States:**
- Initial: dropzone + empty grid
- Uploading: per-file progress bars in grid (signed URL upload direct to Supabase Storage)
- Uploaded: grid of image thumbnails, each selectable as reference
- Reference selected: selected thumbnail has a blue border + "Reference" badge
- Ready: "Next: Annotate" button enabled once reference is selected

**Constraints:**
- Accept: image/jpeg, image/png only
- Max 20 MB per file (validated client-side and server-side)
- HEIC rejected with "Convert to JPG or PNG"
- 10–50 images recommended; no hard limit enforced in UI

**Actions:**
- Drop or click to add files → POST /api/projects/:id/images (one per file) → upload via signed URL → POST /api/projects/:id/images/:imageId/confirm
- Click thumbnail to select reference → PATCH /api/projects/:id (sets reference_image_id)
- "Next: Annotate" → navigate to annotate page

---

### `/projects/[id]/annotate` — Annotation Canvas

**Purpose:** Draw rectangular regions on the reference image and name each field.

See §3 for full canvas spec.

**Actions:**
- Draw boxes on canvas
- Name each field in the sidebar
- Optionally set expected format (number/text/boolean + unit)
- "Save & Annotate" → POST /api/projects/:id/fields → navigate to process page

**Constraints:**
- Max 20 fields
- All fields must be named before saving
- Field names must be unique within the project

---

### `/projects/[id]/process` — Job Progress

**Purpose:** Confirm and start the AI extraction job. Show per-image progress.

See §4 for full processing spec.

**Actions:**
- Page load: show pre-job confirmation modal (if no active job)
- "Start Processing" in modal → POST /api/projects/:id/jobs
- Auto-redirect to /review on full success (after 1.5s delay)
- "Cancel Job" button → DELETE /api/projects/:id/jobs/:jobId

---

### `/projects/[id]/review` — Review Table

**Purpose:** Show extracted values with crop previews. Allow inline corrections.

See §4 for full review table spec.

**Actions:**
- Double-click or Enter on a cell → enter edit mode
- Type new value + Enter or click away → PATCH /api/cell-overrides (upsert)
- "Issues only" toggle → filter to rows with low_confidence/needs_review/failed cells
- "Export CSV" button → navigate to /export

---

### `/projects/[id]/export` — CSV Export

**Purpose:** Download extracted values as CSV. Last step in the flow.

**States:**
- Ready: "Download CSV" button
- Downloading: button shows spinner

**CSV format:** `filename, {field_1}, {field_1}_confidence, {field_1}_status, {field_2}, ...`
- Failed cells: emit `[FAILED]`
- Corrected cells: use `corrected_value` from cell_overrides (not raw AI value)
- needs_review cells: value with `*` suffix in confidence column

**Actions:**
- "Download CSV" → GET /api/projects/:id/export → streamed response, browser downloads

---

## 3. Annotation Canvas {#canvas}

### Modes

**Draw mode** (default on page load):
- Cursor: crosshair
- Mouse drag creates a new rectangle
- On mouse-up: new box added to sidebar with auto-focus on name input
- Minimum box: 20×20 display pixels (smaller boxes are rejected)

**Select mode** (press S or click select icon):
- Cursor: default
- Click existing box to select it (shows Konva Transformer with 8 resize handles)
- Drag to reposition; drag handles to resize
- Delete key removes selected box + removes field from sidebar

### Coordinate Storage

All coordinates are stored **normalized 0..1** against the original image dimensions, not display pixels.

Conversion: `stored_x = canvas_x / (canvas_width / original_width)`

### Field Colors

8-color palette assigned in rotation: `['#EF4444','#F97316','#EAB308','#22C55E','#3B82F6','#8B5CF6','#EC4899','#14B8A6']`

### Sidebar (FieldSidebar)

- One row per drawn box (FieldRow)
- Name input: auto-focused when box is drawn; required, unique
- Expected format: dropdown (number / text / boolean) + optional unit text input
- Delete button: removes box and field

### Save Behavior

"Save & Continue" button:
1. Validates: all boxes named, all names unique, at least 1 field
2. POST /api/projects/:id/fields with full field list
3. On success: navigate to /process

---

## 4. Review Table {#review-table}

### Layout

- Fixed left columns: row number, image thumbnail (48×48px), filename, row status icon
- Dynamic columns: one per template field (ordered by display_order)
- Virtualized rows: @tanstack/react-virtual (64px row height)
- Column headers: field name + "N issues" count badge if any cells in that column have issues

### Cell States

| Status | Appearance |
|---|---|
| ok | Green dot · value text |
| low_confidence | Yellow dot · confidence % · value text |
| needs_review | Orange ⚠ · value text (or validation error hint) |
| failed | Grey italic "Not found" |
| corrected | Blue pencil icon · corrected value (original shown on hover) |

### Crop Preview (Primary Trust Mechanism)

- Each value cell shows a thumbnail of the detected crop inline (small, ~64×64px)
- Hovering the cell expands the crop preview and overlays the detected bounding box on the source image thumbnail in the row's image column
- No crop available for failed cells

### Inline Edit

- **Trigger:** double-click on a value cell OR click to focus then press Enter
- **Edit mode:** input field with current value pre-filled
- **Confirm:** Enter key or click away → PATCH /api/cell-overrides
- **Cancel:** Escape key
- **After save:** cell shows corrected state (blue pencil, corrected value)

### Filtering

- Toggle: "All rows" / "Issues only" (issues = any cell in that row is low_confidence, needs_review, or failed)
- Issues count shown in red badge on toggle button

### Sorting

Default: sorted by filename. Click column header to sort by that field's value (alphabetical for strings, numeric for numbers).

---

## 5. Processing Page {#processing}

### Pre-Job Confirmation Modal (AlertDialog)

Shown on page load if no active job exists for the project.

Content:
```
Ready to process [N] images?

Fields to extract:
• Voltage
• Current
• Resistance
(... up to 5 shown, "+ N more" if > 5)

[Cancel]  [Start Processing]
```

### Image Status List

- Virtualized list, 64px rows, @tanstack/react-virtual
- Each row: status icon + filename + (if done) elapsed time

**Status icons:**
| image_task.status | Icon | Animation |
|---|---|---|
| pending | Clock | Static |
| detecting | Pulsing dot | Pulse loop |
| extracting | Spinner | Spin |
| done | Green checkmark | Static |
| failed | Red X | Static |
| not_found | Grey dash | Static |

### Overall Progress

- Progress bar: `progress_done / progress_total`
- Count line: "12 done · 3 failed · 35 pending"
- Browser tab title: `(12/50) EasyLab — Processing`

### Completion Behavior

- **Full success** (all done, 0 failed): auto-redirect to /review after 1.5s with success toast
- **Partial failure** (some failed): no auto-redirect; show banner "44 succeeded · 3 failed — review results to see which fields were not found"; "Go to Review" button

### Polling

Polls `GET /api/projects/:id/jobs/:jobId` every 1.5s. Phase 6 upgrades this to Supabase Realtime.

---

## 6. Component Hierarchy {#components}

```
app/
├── page.tsx                          → Dashboard
│     ProjectTable
│
├── projects/[id]/
│   ├── page.tsx                      → ProjectHub (stepper)
│   │     ProjectStepper
│   │
│   ├── upload/page.tsx               → UploadPage
│   │     Dropzone
│   │     ImageGrid
│   │       ImageThumbnail (selectable, reference badge)
│   │
│   ├── annotate/page.tsx             → AnnotatePage
│   │     AnnotationCanvas            (react-konva Stage)
│   │       KonvaImage                (reference image layer)
│   │       KonvaRect[]               (drawn boxes layer)
│   │       KonvaTransformer          (select mode handles)
│   │     FieldSidebar
│   │       FieldRow[]
│   │         NameInput
│   │         ExpectedFormatSelect
│   │         UnitInput
│   │         DeleteButton
│   │
│   ├── process/page.tsx              → ProcessPage
│   │     JobConfirmationModal        (shadcn AlertDialog)
│   │     ProgressBar
│   │     StatusCounts
│   │     ImageStatusList             (@tanstack/react-virtual)
│   │       ImageStatusRow
│   │         StatusIcon
│   │
│   ├── review/page.tsx               → ReviewPage
│   │     FilterToggle                ("All" / "Issues only")
│   │     ReviewTable                 (@tanstack/react-table + virtual)
│   │       HeaderRow
│   │         FieldHeader             (name + issue count badge)
│   │       DataRows                  (virtualized)
│   │         ImageCell               (thumbnail + filename)
│   │         ValueCell
│   │           CropPreview           (inline thumbnail, expanded on hover)
│   │           ConfidencePill        (status-based color dot/icon)
│   │           ValueEditor           (shown on double-click/Enter)
│   │
│   └── export/page.tsx               → ExportPage
│         DownloadButton
```

### Component Library

| Library | Usage |
|---|---|
| shadcn/ui | All standard UI components (Button, Input, Dialog, Badge, etc.) |
| Sonner | Toast notifications |
| @tanstack/react-table | Review table state management (sorting, filtering, column defs) |
| @tanstack/react-virtual | Virtualization for review table rows and image status list |
| @tanstack/react-query | Server state, polling, cache invalidation |
| react-konva + konva | Annotation canvas (Stage, Layer, Image, Rect, Transformer) |
| react-dropzone | File upload dropzone |
| Zod | Request body validation in Next.js API routes |

---

## 7. Toast Strategy {#toasts}

### Show a toast for:

| Event | Toast |
|---|---|
| Job started | "Processing started — 47 images queued" (info) |
| Job completed (full success) | "Processing complete — 47 images extracted" (success) |
| Job completed (partial failure) | "Processing done — 44 succeeded, 3 failed" (warning) |
| Job failed entirely | "Processing failed — [error message]" (error) |
| Cell override saved | "Correction saved" (success, 2s auto-dismiss) |
| Export ready / download started | "CSV ready" (success) |
| Quota exceeded | "Daily limit reached — try again tomorrow" (error) |

### Stay silent for:

- Individual image status changes (shown in the virtualized list instead)
- Progress updates (shown in progress bar)
- Upload progress (shown per-file in the grid)
- Polling errors that auto-recover
