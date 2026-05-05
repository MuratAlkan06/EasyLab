# EasyLab — REST API Contract (v1 MVP)

> Generated: 2026-05-04
> Owner labels: **[Next]** = Next.js 15 route handler on Vercel | **[FastAPI]** = FastAPI service on Fly.io
> All request/response bodies are JSON unless noted. All timestamps are ISO 8601 UTC strings.
> Cookie `workspace_id` (HttpOnly, Secure) is required on every Next.js endpoint. FastAPI internal endpoints are not exposed to the public internet.

---

## Table of Contents

1. [Workspaces](#workspaces)
2. [Projects](#projects)
3. [Images](#images)
4. [Fields (Template)](#fields)
5. [Jobs](#jobs)
6. [Cells (Results)](#cells)
7. [Export](#export)
8. [Internal / Infrastructure](#internal)
9. [Shared Error Shape](#error-shape)
10. [Validation Rules Summary](#validation-rules)

---

## Shared Error Shape {#error-shape}

Every error response uses this envelope:

```
{
  "error": {
    "code":    string,   // machine-readable slug, e.g. "not_found", "conflict"
    "message": string    // human-readable
  }
}
```

Common codes used throughout:

| HTTP | code | Meaning |
|---|---|---|
| 400 | validation_error | Request body fails schema or business rule |
| 404 | not_found | Resource does not exist or does not belong to this workspace |
| 409 | conflict | Uniqueness violation (e.g., duplicate field name, active job exists) |
| 422 | unprocessable | Structurally valid but semantically wrong (e.g., no fields defined) |
| 429 | quota_exceeded | Per-workspace daily quota or global token budget exceeded |
| 500 | internal_error | Unexpected server error |

---

## 1. Workspaces {#workspaces}

### POST /api/workspaces [Next]

Creates a new workspace row. Called once on first page load if the cookie is absent. The client should not call this if the cookie already exists.

**Request body:** none

**Response 201:**
```
{
  "workspace_id": uuid
}
```
Sets `Set-Cookie: workspace_id=<uuid>; HttpOnly; Secure; SameSite=Lax; Path=/`

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 500 | internal_error | DB insert failed |

**Tables touched:** `workspaces` (INSERT)

---

## 2. Projects {#projects}

### POST /api/projects [Next]

Creates a new project owned by the workspace from the cookie.

**Request body:**
```
{
  "name": string   // required; 1–120 chars; unique within workspace
}
```

**Response 201:**
```
{
  "id":         uuid,
  "name":       string,
  "status":     "draft",
  "created_at": timestamp
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 400 | validation_error | name is missing, empty, or > 120 chars |
| 409 | conflict | name already exists in this workspace |

**Tables touched:** `projects` (INSERT)

---

### GET /api/projects [Next]

Lists all projects for the current workspace, newest first.

**Query params:** none (no pagination in MVP)

**Response 200:**
```
{
  "projects": [
    {
      "id":                 uuid,
      "name":               string,
      "status":             "draft"|"annotated"|"processing"|"done",
      "image_count":        int,     // count of images excluding pending_upload
      "reference_image_id": uuid|null,
      "created_at":         timestamp,
      "updated_at":         timestamp
    }
  ]
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 500 | internal_error | DB query failed |

**Tables touched:** `projects`, `images` (SELECT with count)

---

### GET /api/projects/:id [Next]

Returns project detail plus image and field counts.

**Response 200:**
```
{
  "id":                  uuid,
  "name":                string,
  "status":              string,
  "reference_image_id":  uuid|null,
  "image_count":         int,
  "field_count":         int,
  "has_active_job":      boolean,
  "latest_job_id":       uuid|null,   // most recent job regardless of status
  "created_at":          timestamp,
  "updated_at":          timestamp
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 404 | not_found | project does not exist or belongs to different workspace |

**Tables touched:** `projects`, `images`, `template_fields`, `jobs` (SELECT)

---

### DELETE /api/projects/:id [Next]

Deletes the project and all cascaded rows (images, fields, jobs, cells). Also deletes Supabase Storage objects under the project prefix before the DB row is deleted.

**Request body:** none

**Response 204:** no body

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 404 | not_found | project not found for this workspace |
| 409 | conflict | project has a running job; must wait or cancel first |

**Tables touched:** `projects` (DELETE, cascades to all child tables)
**State transitions:** none (cascade delete is not a state transition)

---

## 3. Images {#images}

### POST /api/projects/:id/upload-urls [Next]

Requests N signed upload URLs from Supabase Storage. Server decides the storage paths; returns them to the client alongside the signed tokens. Client uploads directly to Supabase Storage using these tokens (no bytes go through Next.js).

Uses Supabase Storage `createSignedUploadUrl` per file.

**Request body:**
```
{
  "files": [
    {
      "filename":  string,   // required; original filename
      "mime_type": string,   // required; must be image/jpeg, image/png, image/webp, image/tiff
      "size_bytes": int      // required; > 0, <= 52_428_800 (50 MB)
    }
  ]
}
```
Max 200 files per call. `files` must have at least 1 entry.

**Response 201:**
```
{
  "uploads": [
    {
      "image_id":     uuid,          // newly created images row id
      "storage_path": string,        // e.g. "projects/{proj_id}/{image_id}.jpg"
      "upload_url":   string,        // Supabase signed upload URL
      "token":        string,        // x-signature token for uploadToSignedUrl
      "filename":     string
    }
  ]
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 400 | validation_error | files array empty, > 200 entries, invalid mime_type, size_bytes out of range |
| 404 | not_found | project not found for this workspace |
| 409 | conflict | project has a running or queued job |

**Tables touched:** `images` (INSERT one row per file with status=pending_upload)
**State transitions:** images created in `pending_upload`

---

### POST /api/projects/:id/images/:imageId/confirm [Next]

Called by the client after each image upload completes (Supabase Storage SDK signals completion). Marks the image as uploaded and optionally records metadata resolved client-side.

**Request body:**
```
{
  "size_bytes": int,   // optional; actual bytes received
  "width_px":  int,   // optional; image dimensions if decoded client-side
  "height_px": int    // optional
}
```

**Response 200:**
```
{
  "image_id": uuid,
  "status":   "uploaded"
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 404 | not_found | image not found or wrong project/workspace |
| 409 | conflict | image is not in pending_upload status (e.g., already confirmed) |

**Tables touched:** `images` (UPDATE status → uploaded)
**State transitions:** `pending_upload` → `uploaded`

---

### GET /api/projects/:id/images [Next]

Lists all images for the project. Returns signed read URLs for display in the UI (short-lived, 1 hour).

**Query params:**
- `status` (optional): filter by status value

**Response 200:**
```
{
  "images": [
    {
      "id":           uuid,
      "filename":     string,
      "status":       string,
      "is_reference": boolean,
      "size_bytes":   int|null,
      "width_px":     int|null,
      "height_px":    int|null,
      "signed_url":   string,   // 1-hour signed URL for display
      "created_at":   timestamp
    }
  ]
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 404 | not_found | project not found for this workspace |

**Tables touched:** `images` (SELECT)

---

### DELETE /api/images/:id [Next]

Deletes one image from DB and from Supabase Storage. Rejects if the image is the current reference image (must unset reference first) or if a job is active.

**Response 204:** no body

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 404 | not_found | image not found or wrong workspace |
| 409 | conflict | image is the reference image for its project |
| 409 | conflict | project has an active job (queued or running) |

**Tables touched:** `images` (DELETE)

---

### PUT /api/projects/:id/reference [Next]

Sets the reference image for the project. Clears the previous reference (sets `is_reference = false` on old row). Updates `projects.reference_image_id`.

**Request body:**
```
{
  "image_id": uuid   // required; must belong to this project and be in uploaded/done status
}
```

**Response 200:**
```
{
  "project_id":          uuid,
  "reference_image_id":  uuid
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 400 | validation_error | image_id missing |
| 404 | not_found | project or image not found for this workspace |
| 422 | unprocessable | image status is pending_upload or failed |

**Tables touched:** `images` (UPDATE is_reference), `projects` (UPDATE reference_image_id)
**State transitions:** none (is_reference is a flag, not a status machine)

---

## 4. Fields (Template) {#fields}

### POST /api/projects/:id/fields [Next]

Saves the full field annotation list. This is a **replace** operation: the submitted array becomes the authoritative field list. Existing fields not present in the submission are deleted. Existing fields present by `field_name` are updated. New names are inserted.

This operation runs inside a transaction.

**Request body:**
```
{
  "fields": [
    {
      "field_name":            string,   // required; 1–100 chars
      "reference_box":         {         // required
        "x":      float,                 // 0..1 normalized
        "y":      float,                 // 0..1 normalized
        "width":  float,                 // > 0, x+width <= 1
        "height": float                  // > 0, y+height <= 1
      },
      "semantic_description":  string,   // optional; max 500 chars
      "expected_format":       {         // optional; omit if unknown
        "type": "number"|"text"|"boolean",
        "unit": string|null
      }
    }
  ]
}
```

Constraints:
- `fields` must have at least 1 entry and at most 50 entries.
- `field_name` must be unique within the submitted array.
- `reference_box` values must be normalized 0..1: x and y >= 0, width and height > 0, x+width <= 1, y+height <= 1.

**Response 200:**
```
{
  "fields": [
    {
      "id":                    uuid,
      "field_name":            string,
      "display_order":         int,
      "reference_box":         object,
      "semantic_description":  string|null,
      "expected_format":       object|null
    }
  ]
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 400 | validation_error | fields array empty or > 50, missing required fields, bad box coords |
| 404 | not_found | project not found for this workspace |
| 422 | unprocessable | project has no reference image set yet |
| 409 | conflict | project has an active job |

**Tables touched:** `template_fields` (DELETE orphans + UPSERT), `projects` (UPDATE status → annotated if >= 1 field saved)
**State transitions:** project status `draft` → `annotated` on first successful save

---

### GET /api/projects/:id/fields [Next]

Returns all fields for the project, ordered by `display_order`.

**Response 200:**
```
{
  "fields": [
    {
      "id":                   uuid,
      "field_name":           string,
      "display_order":        int,
      "reference_box":        object,
      "semantic_description": string|null,
      "expected_format":      object|null,
      "updated_at":           timestamp
    }
  ]
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 404 | not_found | project not found for this workspace |

**Tables touched:** `template_fields` (SELECT)

---

## 5. Jobs {#jobs}

### POST /api/projects/:id/jobs [Next]

Enqueues a batch processing job. Next.js validates preconditions, inserts the job row, then calls `POST /internal/kick` on FastAPI to wake the worker.

**Request body:** none (all configuration comes from DB state)

**Preconditions (validated by Next.js before insert):**
1. Project has a reference image set (`reference_image_id IS NOT NULL`)
2. Project has >= 1 `template_fields` row
3. Project has >= 2 images in `uploaded` or `done` status
4. No job with status `queued` or `running` exists for this project

**Response 202:**
```
{
  "job_id":         uuid,
  "status":         "queued",
  "progress_total": int,     // number of images that will be processed
  "created_at":     timestamp
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 404 | not_found | project not found for this workspace |
| 422 | unprocessable | no reference image set |
| 422 | unprocessable | no template fields defined |
| 422 | unprocessable | fewer than 2 uploaded images |
| 409 | conflict | active job already exists for this project |

**Tables touched:** `jobs` (INSERT), `image_tasks` (INSERT one per uploaded image), `images` (SELECT count)
**State transitions:** job created in `queued`; image_tasks created in `pending`

---

### GET /api/projects/:id/jobs/:jobId [Next]

Returns job status and per-image task breakdown. Used for polling the progress indicator.

**Response 200:**
```
{
  "job": {
    "id":             uuid,
    "status":         string,
    "progress_total": int,
    "progress_done":  int,
    "attempts":       int,
    "error":          string|null,
    "created_at":     timestamp,
    "started_at":     timestamp|null,
    "finished_at":    timestamp|null
  },
  "tasks": [
    {
      "image_id":  uuid,
      "filename":  string,
      "status":    string,
      "error":     string|null,
      "updated_at": timestamp
    }
  ]
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 404 | not_found | job not found or belongs to different project/workspace |

**Tables touched:** `jobs`, `image_tasks`, `images` (SELECT)

---

### DELETE /api/projects/:id/jobs/:jobId [Next → FastAPI]

Cancels a queued or running job. Next.js validates ownership, then calls `POST /internal/jobs/:jobId/cancel` on FastAPI. FastAPI sets `status = 'cancelled'` and `finished_at = now()`. The worker checks for cancellation between pipeline stages (after each image completes) and stops processing.

**Request body:** none

**Response 200:**
```
{
  "job_id": uuid,
  "status": "cancelled"
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 404 | not_found | job not found or belongs to different project/workspace |
| 409 | conflict | job is already in a terminal state (succeeded, failed, cancelled) |

**Tables touched:** `jobs` (UPDATE status → cancelled)
**State transition:** queued or running → cancelled

---

## 6. Cells (Results) {#cells}

### GET /api/projects/:id/cells [Next]

Returns the full results table for the latest succeeded job. Response is structured as a 2D grid keyed by image and field for easy rendering.

**Query params:**
- `job_id` (optional): specify a job explicitly; defaults to latest succeeded job

**Response 200:**
```
{
  "job_id":  uuid,
  "fields":  [
    { "id": uuid, "field_name": string, "display_order": int }
  ],
  "rows": [
    {
      "image_id":  uuid,
      "filename":  string,
      "signed_url": string,   // 1-hour signed URL for thumbnail
      "cells": [
        {
          "id":                  uuid,
          "field_id":            uuid,
          "raw_text":            string|null,
          "parsed_value":        any|null,     // JSON scalar
          "unit_seen":           string|null,
          "combined_confidence": number,       // 0.0–1.0
          "status":              string,
          "failure_reason":      string|null,
          "validation_error":    string|null,
          "corrected_value":     any|null,
          "corrected_at":        timestamp|null,
          "detected_box":        object|null,
          "crop_path":           string|null
        }
      ]
    }
  ]
}
```

`rows` are ordered by image filename ascending. `cells` within each row are ordered by `display_order` to match `fields` array index.

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 404 | not_found | project not found for this workspace |
| 404 | not_found | no succeeded job exists for this project (or specified job_id not found) |

**Tables touched:** `jobs`, `cells`, `template_fields`, `images` (SELECT)

---

### PATCH /api/cells/:id [Next]

Inline user correction for a single cell. Records the corrected value alongside the original AI output (non-destructive).

**Request body:**
```
{
  "corrected_value": any   // required; JSON scalar matching expected_format.type; null to clear
}
```

Validation: if the field has `expected_format.type = "number"`, `corrected_value` must be a JSON number or null. Same for boolean. Text accepts any string or null.

**Response 200:**
```
{
  "id":              uuid,
  "corrected_value": any|null,
  "corrected_at":    timestamp|null
}
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 400 | validation_error | corrected_value type does not match expected_format.type |
| 404 | not_found | cell not found or does not belong to this workspace |

**Tables touched:** `cells` (UPDATE corrected_value, corrected_at, updated_at), `template_fields` (SELECT for type validation)

---

## 7. Export {#export}

### GET /api/projects/:id/export.csv [Next]

Streams a CSV file. Columns: filename, then one column per field in `display_order`. Cell value is `corrected_value` if set, else `parsed_value`. Confidence and status columns are appended after the value columns.

**Query params:**
- `job_id` (optional): specify job; defaults to latest succeeded

**Response 200:**
```
Content-Type: text/csv
Content-Disposition: attachment; filename="easylab-{project_name}-{date}.csv"
```

CSV structure:
```
filename, {field_1}, {field_1}_confidence, {field_1}_status, {field_2}, ...
image_a.jpg, 12.3, 0.91, ok, ...
image_b.jpg, [FAILED], 0.00, failed, ...
```

Failed cells emit the literal string `[FAILED]`. Needs_review cells emit the value with a `*` suffix in the confidence column (e.g., `0.38*`).

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 404 | not_found | project not found or no succeeded job |

**Tables touched:** `jobs`, `cells`, `template_fields`, `images` (SELECT)

---

## 8. Internal / Infrastructure {#internal}

These routes are on the FastAPI service and are NOT exposed through Next.js or to the public internet. They are called server-to-server only (Next.js → FastAPI using a shared secret header `X-Internal-Secret`).

### GET /health [FastAPI]

Liveness probe used by Fly.io and monitoring.

**Response 200:**
```
{ "status": "ok", "timestamp": timestamp }
```

**No error cases** (if this returns non-200, the instance is considered dead).

**Tables touched:** none

---

### POST /internal/kick [FastAPI]

Wakes the job worker. Called by Next.js after inserting a job. The worker polls for the oldest `queued` job. This endpoint just ensures the worker loop is awake; it is idempotent.

**Request body:**
```
{ "job_id": uuid }   // hint; worker validates and re-queries DB
```

**Response 202:**
```
{ "accepted": true }
```

**Error cases:**

| Status | code | Condition |
|---|---|---|
| 401 | unauthorized | X-Internal-Secret header missing or wrong |

**Tables touched:** `jobs` (SELECT — worker polls separately)

---

## 9. Validation Rules Summary {#validation-rules}

### POST /api/projects
- `name`: string, required, 1–120 chars, unique within workspace

### POST /api/projects/:id/upload-urls
- `files`: array, 1–200 entries
- `files[].filename`: string, required, non-empty
- `files[].mime_type`: one of `image/jpeg`, `image/png`, `image/webp`, `image/tiff`
- `files[].size_bytes`: integer, 1 – 52_428_800 (50 MB)

### POST /api/projects/:id/images/:imageId/confirm
- All fields optional; if provided: size_bytes > 0, width_px > 0, height_px > 0

### PUT /api/projects/:id/reference
- `image_id`: uuid, required
- Image must belong to this project, status must be `uploaded` or `done`

### POST /api/projects/:id/fields
- `fields`: array, 1–50 entries
- `fields[].field_name`: string, required, 1–100 chars, unique within submitted array
- `fields[].reference_box.x`: float, 0..1 normalized, >= 0
- `fields[].reference_box.y`: float, 0..1 normalized, >= 0
- `fields[].reference_box.width`: float, > 0, x+width <= 1
- `fields[].reference_box.height`: float, > 0, y+height <= 1
- `fields[].semantic_description`: string, optional, max 500 chars
- `fields[].expected_format.type`: one of `"number"`, `"text"`, `"boolean"` if key is present

### POST /api/projects/:id/jobs (business rule preconditions)
1. `projects.reference_image_id IS NOT NULL`
2. `COUNT(template_fields WHERE project_id = ?) >= 1`
3. `COUNT(images WHERE project_id = ? AND status IN ('uploaded','done')) >= 2`
4. `NOT EXISTS (SELECT 1 FROM jobs WHERE project_id = ? AND status IN ('queued','running'))`

### PATCH /api/cells/:id
- `corrected_value`: required key (value may be null to clear)
- Type must match `template_fields.expected_format.type` when that field is set:
  - `number` → JSON number or null
  - `boolean` → JSON boolean or null
  - `text` → any string or null
  - If `expected_format` is null on the field, any scalar JSON value is accepted
