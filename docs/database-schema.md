# EasyLab — PostgreSQL Schema (v1 MVP)

> Generated: 2026-05-04
> All tables live in Supabase Postgres (public schema). asyncpg is used from FastAPI; @supabase/supabase-js from Next.js server routes.

---

## Table of Contents

1. [Schema — All Tables](#schema)
2. [State Machines](#state-machines)
3. [JSONB vs Normalized Decisions](#jsonb-vs-normalized)
4. [Indexes Summary](#indexes-summary)
5. [Assumptions](#assumptions)

Tables: workspaces · projects · images · template_fields · jobs · image_tasks · cells · cell_overrides · workspace_quota

---

## 1. Schema {#schema}

### 1.1 workspaces

```sql
create table workspaces (
  id         uuid        primary key default gen_random_uuid(),
  created_at timestamptz not null    default now()
);
```

No name, no user. One row is created on first visit and the UUID is dropped into a secure HttpOnly cookie (`workspace_id`). No columns beyond identity and timestamp.

Scale note: when auth is added later, add `user_id uuid references auth.users(id)` and migrate cookie sessions by linking on first login.

---

### 1.2 projects

```sql
create table projects (
  id                 uuid        primary key default gen_random_uuid(),
  workspace_id       uuid        not null references workspaces(id) on delete cascade,
  name               text        not null,
  reference_image_id uuid,       -- FK set after first upload; circular ref handled below
  status             text        not null default 'draft',
                                  -- draft | annotated | processing | done
  template_quality   text,                    -- null | 'degraded'; set by FastAPI during template generation
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),

  constraint projects_name_workspace_unique unique (workspace_id, name),
  constraint projects_status_check check (
    status in ('draft','annotated','processing','done')
  )
);

-- reference_image_id FK is added after images table is created (see migrations section)
-- alter table projects
--   add constraint fk_projects_reference_image
--   foreign key (reference_image_id) references images(id) on delete set null;
```

Assumption: project names are unique within a workspace but not globally.
Assumption: `reference_image_id` is nullable until the user explicitly sets a reference image.

---

### 1.3 images

```sql
create table images (
  id           uuid        primary key default gen_random_uuid(),
  project_id   uuid        not null references projects(id) on delete cascade,
  storage_path   text        not null,   -- Supabase Storage object path, e.g. projects/{proj_id}/originals/{uuid}.jpg
  thumbnail_path text,                  -- set by FastAPI after thumbnail is generated; projects/{proj_id}/thumbs/{uuid}.jpg
  filename       text        not null,  -- original client filename for display
  mime_type    text        not null,   -- e.g. image/jpeg, image/png
  size_bytes   bigint,                 -- nullable until confirmed
  width_px     int,                    -- nullable until confirmed
  height_px    int,                    -- nullable until confirmed
  status       text        not null default 'pending_upload',
                            -- pending_upload | uploaded | processing | done | failed
  is_reference boolean     not null default false,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),

  constraint images_status_check check (
    status in ('pending_upload','uploaded','processing','done','failed')
  )
);

-- Enforce exactly one reference image per project at the DB level
create unique index uq_images_one_reference_per_project
  on images (project_id)
  where is_reference = true;

-- Fast lookup of all images for a project (used constantly)
create index ix_images_project_id on images (project_id);
```

Assumption: `storage_path` is always set at row creation (before upload), because the signed URL is generated first and the path is decided server-side.
Assumption: `size_bytes`, `width_px`, `height_px` are filled in at confirm time.

---

### 1.4 template_fields

```sql
create table template_fields (
  id                    uuid        primary key default gen_random_uuid(),
  project_id            uuid        not null references projects(id) on delete cascade,
  field_name            text        not null,
  display_order         int         not null default 0,   -- UI column ordering
  reference_box         jsonb       not null,
  -- shape: {"x": float, "y": float, "width": float, "height": float} normalized 0..1
  semantic_description  text,                             -- nullable; used in AI prompt
  expected_format       jsonb,
  -- shape: {"type": "number"|"text"|"boolean", "unit": "V"|"A"|null}
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),

  constraint uq_template_fields_name_per_project unique (project_id, field_name),
  constraint template_fields_field_name_nonempty check (char_length(field_name) > 0)
);

-- Fast lookup by project (used on every extraction and export)
create index ix_template_fields_project_id on template_fields (project_id);
```

---

### 1.5 jobs (already decided — reproduced for completeness)

```sql
create table jobs (
  id              uuid        primary key default gen_random_uuid(),
  project_id      uuid        not null references projects(id) on delete cascade,
  kind            text        not null default 'process_batch',
  status          text        not null default 'queued',
                               -- queued | running | succeeded | failed | cancelled
  payload         jsonb       not null default '{}',
  attempts        int         not null default 0,
  max_attempts    int         not null default 3,
  error           text,
  progress_total  int         not null default 0,
  progress_done   int         not null default 0,
  created_at      timestamptz not null default now(),
  started_at      timestamptz,
  finished_at     timestamptz,
  heartbeat_at    timestamptz,
  paused_until    timestamptz,    -- set by worker on 429 or circuit breaker; worker skips job until now() > paused_until
  locked_by       text,

  constraint jobs_status_check check (
    status in ('queued','running','succeeded','failed','cancelled')
  )
);

-- Exactly one active job per project at a time
create unique index uq_jobs_one_active_per_project
  on jobs (project_id)
  where status in ('queued','running');

-- Polling query from FastAPI worker: "give me the oldest queued job"
create index ix_jobs_status_created on jobs (status, created_at)
  where status = 'queued';
```

---

### 1.6 image_tasks (already decided — reproduced for completeness)

```sql
create table image_tasks (
  id         uuid        primary key default gen_random_uuid(),
  job_id     uuid        not null references jobs(id) on delete cascade,
  image_id   uuid        not null references images(id) on delete cascade,
  status     text        not null default 'pending',
              -- pending | detecting | extracting | done | failed | not_found
  error      text,
  updated_at timestamptz not null default now(),

  constraint uq_image_tasks_job_image unique (job_id, image_id),
  constraint image_tasks_status_check check (
    status in ('pending','detecting','extracting','done','failed','not_found')
  )
);

create index ix_image_tasks_job_id on image_tasks (job_id);
```

---

### 1.7 cells

This is the core result table. One row per (job, image, template_field) tuple.

```sql
create table cells (
  id                  uuid        primary key default gen_random_uuid(),

  -- Ownership
  job_id              uuid        not null references jobs(id) on delete cascade,
  project_id          uuid        not null references projects(id) on delete cascade,
  image_id            uuid        not null references images(id) on delete cascade,
  field_id            uuid        not null references template_fields(id) on delete cascade,

  -- Raw AI output
  raw_text            text,
  parsed_value        jsonb,
  -- shape: scalar — number | string | boolean | null
  unit_seen           text,
  legible             boolean,    -- false when Gemini Flash/PaddleOCR cannot read the crop; forces needs_review

  -- Confidence
  box_confidence      numeric(4,3) not null default 0,  -- 0.000 to 1.000
  value_confidence    numeric(4,3) not null default 0,
  combined_confidence numeric(4,3),  -- written by worker: round(0.4*box + 0.6*value, 3)

  -- Status
  status              text        not null default 'ok',
                       -- ok | low_confidence | needs_review | failed
  failure_reason      text,
  validation_error    text,

  -- Spatial
  detected_box        jsonb,
  -- shape: {"x": float, "y": float, "width": float, "height": float} normalized 0..1
  crop_path           text,       -- Supabase Storage path to cropped region image

  -- Provenance
  model_versions      jsonb       not null default '{}',
  -- shape: {"detector": "gemini-2.0-flash", "extractor": "gemini-2.0-flash"}
  retried             boolean     not null default false,

  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),

  constraint uq_cells_job_image_field unique (job_id, image_id, field_id),
  constraint cells_status_check check (
    status in ('ok','low_confidence','needs_review','failed')
  ),
  constraint cells_box_confidence_range check (box_confidence between 0 and 1),
  constraint cells_value_confidence_range check (value_confidence between 0 and 1)
);

-- Primary access pattern: "get all cells for a project's latest job" (review table)
create index ix_cells_job_id on cells (job_id);

-- Cross-job queries (e.g. job history comparison)
create index ix_cells_project_id on cells (project_id);

-- Export and per-image drill-down
create index ix_cells_image_id on cells (image_id);

-- Filtering cells by review status (review table UI filters)
create index ix_cells_job_status on cells (job_id, status)
  where status in ('low_confidence','needs_review','failed');
```

Assumption: `combined_confidence` is a regular column written by the worker (`round(0.4 * box_confidence + 0.6 * value_confidence, 3)`). Not a generated column — keeping the worker in control avoids the Postgres restriction on writing generated columns directly.
Assumption: one job produces at most one cell per (image, field). Re-runs create a new job, producing a new set of cells. Old cells are still accessible via job history.

---

### 1.8 cell_overrides

User corrections that survive job re-runs. Keyed by `(project_id, image_id, field_id)` — never by `job_id`. The `cells` table is write-once AI output; this table holds the human layer on top.

```sql
create table cell_overrides (
  id              uuid        primary key default gen_random_uuid(),
  project_id      uuid        not null references projects(id) on delete cascade,
  image_id        uuid        not null references images(id) on delete cascade,
  field_id        uuid        not null references template_fields(id) on delete cascade,
  corrected_value jsonb       not null,
  corrected_at    timestamptz not null default now(),

  constraint uq_cell_overrides_project_image_field unique (project_id, image_id, field_id)
);

create index ix_cell_overrides_project_id on cell_overrides (project_id);
```

Assumption: one override per (project, image, field). A second correction from the user replaces the previous one (upsert on the unique key).

---

### 1.9 workspace_quota

Per-workspace daily usage counters. Checked at job enqueue by Next.js (returns 429 if exceeded). Reset daily by comparing `quota_date` to `current_date`.

```sql
create table workspace_quota (
  workspace_id  uuid   primary key references workspaces(id) on delete cascade,
  jobs_today    int    not null default 0,
  images_today  int    not null default 0,
  tokens_today  bigint not null default 0,
  quota_date    date   not null default current_date
);
```

Limits enforced (from env vars):

| Env var | Default | Scope |
|---|---|---|
| MAX_JOBS_PER_WORKSPACE_PER_DAY | 5 | Per workspace |
| MAX_IMAGES_PER_WORKSPACE_PER_DAY | 200 | Per workspace |
| GLOBAL_DAILY_TOKEN_BUDGET | 50,000,000 | Across all workspaces |

Row is upserted on first job of the day. If `quota_date < current_date`, counters reset to 0 before check.

---

## 2. State Machines {#state-machines}

### 2.1 images.status

```
pending_upload  ──[confirm upload: Next.js]──►  uploaded
                                                    │
                                          [job starts: FastAPI]
                                                    │
                                                    ▼
                                               processing
                                             /             \
                              [task done: FastAPI]    [task failed: FastAPI]
                                         /                       \
                                        ▼                         ▼
                                       done                     failed
```

| Transition | From | To | Triggered by | Owner |
|---|---|---|---|---|
| Upload confirmed | pending_upload | uploaded | POST /confirm | Next.js |
| Job picks up image | uploaded | processing | job worker starts image_task | FastAPI |
| All fields extracted | processing | done | image_task → done | FastAPI |
| Unrecoverable error | processing | failed | image_task → failed | FastAPI |

Terminal states: `done`, `failed`

---

### 2.2 jobs.status

```
queued  ──[worker polls]──►  running  ──[all tasks done]──►  succeeded
                                │
                          [error, attempts < max]──►  queued  (retry, attempts++)
                                │
                          [attempts >= max]──►  failed
                                │
                          [user cancels]──►  cancelled
```

| Transition | From | To | Condition | Owner |
|---|---|---|---|---|
| Worker picks up | queued | running | locked_by set, attempts++ | FastAPI |
| All image_tasks succeeded | running | succeeded | progress_done == progress_total | FastAPI |
| Retriable error | running | queued | attempts < max_attempts | FastAPI |
| Max retries exhausted | running | failed | attempts >= max_attempts | FastAPI |
| User requests cancel | queued or running | cancelled | explicit API call | Next.js / FastAPI |

Terminal states: `succeeded`, `failed`, `cancelled`

---

### 2.3 image_tasks.status

```
pending  ──[worker starts]──►  detecting  ──[box found]──►  extracting  ──[value parsed]──►  done
                                    │                              │
                              [box not found]              [parse failed]
                                    │                              │
                                    ▼                              ▼
                                not_found                       failed
```

| Transition | From | To | Condition | Owner |
|---|---|---|---|---|
| Worker starts | pending | detecting | worker begins region detection | FastAPI |
| Box found | detecting | extracting | detector returns bounding box | FastAPI |
| Box not found | detecting | not_found | detector returns nothing | FastAPI |
| Value extracted | extracting | done | extractor returns valid JSON | FastAPI |
| Value extraction fails | extracting | failed | AI error or JSON invalid | FastAPI |

Terminal states: `done`, `not_found`, `failed`

---

### 2.4 cells.status

Set at cell creation by FastAPI. Never transitions after write (user corrections go to `cell_overrides`, not here).

combined_confidence = round(0.4 × box_confidence + 0.6 × value_confidence, 3)

| Status | Condition |
|---|---|
| ok | combined_confidence >= 0.85 AND legible = true AND no validation_error |
| low_confidence | combined_confidence in [0.60, 0.85) |
| needs_review | combined_confidence < 0.60 OR legible = false OR validation_error is not null |
| failed | image_task failed (found=false) or parsed_value null after retry |

`failed` cells are written even when parsing fails so the review table has a complete row for every (image × field) pair.

---

## 3. JSONB vs Normalized Decisions {#jsonb-vs-normalized}

### reference_box and detected_box — JSONB (recommended)

Kept as JSONB `{"x": int, "y": int, "width": int, "height": int}`. Rationale:

- These four values are always read and written together. There is no query filtering on individual coordinates in MVP.
- Four separate columns (`box_x`, `box_y`, `box_width`, `box_height`) would add noise to the schema for zero query benefit.
- JSONB is type-checked at the API boundary (Pydantic model) before insert; no untrusted JSON is stored raw.

Flag for later: if a spatial query ever needs to be done (e.g., "find all fields whose box overlaps region R"), migrate to four int columns or a `box` / `point` composite type. That migration is non-destructive.

---

### expected_format — JSONB (recommended)

Shape is `{"type": "number"|"text"|"boolean", "unit": "V"|null}`. Rationale:

- The field count is small (MVP fields per project is unlikely to exceed 20).
- The format schema is defined by the product and will evolve (min/max range is explicitly out of MVP but will be added). JSONB absorbs schema additions without migrations.
- Not queried by field — only read to build the AI prompt and to run validation.

---

### model_versions — JSONB (recommended)

Shape is `{"detector": "gemini-2.0-flash-001", "extractor": "gemini-2.0-flash-001"}`. Rationale:

- Number of model roles is unknown and will grow (e.g., OCR fallback adds a third key).
- Never filtered or grouped in MVP queries.
- Normalizing into a separate `cell_model_versions` table would require a join on every export query for no gain.

---

### parsed_value — JSONB (recommended)

Scalar JSONB that holds `float`, `int`, `string`, `boolean`, or `null`. Rationale:

- A single typed column cannot hold heterogeneous value types across fields within the same project.
- Alternatives rejected: `value_text text` + `value_number numeric` + `value_boolean boolean` with application-side selection logic — more complex than JSONB with no query advantage.
- Export (CSV) reads `parsed_value` in Python where type coercion is trivial.
- Pydantic validates the value type against `expected_format.type` before the cell is written.

---

## 4. Indexes Summary {#indexes-summary}

| Index | Table | Columns / Condition | Purpose |
|---|---|---|---|
| uq_images_one_reference_per_project | images | project_id WHERE is_reference=true | Enforce single reference image |
| ix_images_project_id | images | project_id | List images for a project |
| ix_template_fields_project_id | template_fields | project_id | Load fields for extraction prompt |
| uq_jobs_one_active_per_project | jobs | project_id WHERE status IN (queued,running) | Prevent double-submission |
| ix_jobs_status_created | jobs | status, created_at WHERE status=queued | Worker poll query |
| ix_image_tasks_job_id | image_tasks | job_id | Load task list for job status endpoint |
| ix_cells_job_id | cells | job_id | Load all cells for review table |
| ix_cells_project_id | cells | project_id | Cross-job queries (job history comparison) |
| ix_cells_image_id | cells | image_id | Per-image drill-down |
| ix_cells_job_status | cells | job_id, status WHERE low_confidence or needs_review or failed | Filter uncertain cells |
| ix_cell_overrides_project_id | cell_overrides | project_id | Load all overrides for a project on export/review |

---

## 5. Assumptions {#assumptions}

1. No auth in v1. `workspace_id` from cookie is trusted as the ownership boundary. Any request with a valid workspace_id cookie can read/write that workspace's data.
2. One active job per project means "queued or running". Succeeded/failed/cancelled jobs accumulate as history.
3. Re-running a batch creates a new job and new cells. The review table always reads from the latest succeeded job.
4. `images.storage_path` is determined server-side before the signed URL is returned to the client. The client uploads to that path; it does not choose the path.
5. `template_fields` rows are replaced wholesale on `POST /api/projects/:id/fields` (upsert by field_name, delete orphans). Field identity is by `field_name` within a project, not by UUID, from the user's perspective.
6. Cells are write-once by FastAPI. User corrections go to `cell_overrides` (keyed by project_id, image_id, field_id). The original AI output in `cells` is never modified. On export, `corrected_value` from `cell_overrides` takes precedence if present.
7. `display_order` on `template_fields` is set by the client as the index of the field in the submitted array.
