# EasyLab — Alembic Migration Strategy (v1 MVP)

> Generated: 2026-05-04

---

## Overview

Alembic is used for all schema changes. FastAPI uses asyncpg directly (no ORM), so Alembic is configured with an async engine pointing at the same Supabase Postgres connection string. SQLAlchemy Core models are defined only for the purpose of autogenerate; no ORM session is used at runtime.

---

## env.py: Async Engine Configuration

Alembic's `env.py` must use `async_engine_from_config` and `run_sync` to support asyncpg:

```python
# alembic/env.py (structure only — not implementation code)
# Uses: sqlalchemy.ext.asyncio.async_engine_from_config
# DATABASE_URL must use postgresql+asyncpg:// scheme
# do_run_migrations(connection) calls context.run_migrations()
# run_migrations_online() calls asyncio.run(run_async_migrations())
```

The `DATABASE_URL` environment variable uses `postgresql+asyncpg://...` for the async driver.

---

## File Naming Convention

```ini
# alembic.ini
file_template = %%(year)d%%(month).2d%%(day).2d_%%(rev)s_%%(slug)s
```

Example: `20260504_a1b2c3d4_create_workspaces.py`

This makes migration order visually obvious and avoids relying solely on the down_revision chain when reading the `versions/` directory.

---

## Migration Order (v1)

All migrations have `down_revision` pointing to the previous migration, forming a linear chain. No branches in v1.

### Migration 001 — create_workspaces

```sql
create table workspaces (
  id         uuid        primary key default gen_random_uuid(),
  created_at timestamptz not null default now()
);
```

No dependencies.

---

### Migration 002 — create_projects

```sql
create table projects (
  id                 uuid        primary key default gen_random_uuid(),
  workspace_id       uuid        not null references workspaces(id) on delete cascade,
  name               text        not null,
  reference_image_id uuid,       -- FK added in migration 004
  status             text        not null default 'draft',
  template_quality   text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  constraint projects_name_workspace_unique unique (workspace_id, name),
  constraint projects_status_check check (status in ('draft','annotated','processing','done'))
);
```

Depends on: 001

---

### Migration 003 — create_images

```sql
create table images (
  id           uuid        primary key default gen_random_uuid(),
  project_id   uuid        not null references projects(id) on delete cascade,
  storage_path   text        not null,
  thumbnail_path text,
  filename       text        not null,
  mime_type    text        not null,
  size_bytes   bigint,
  width_px     int,
  height_px    int,
  status       text        not null default 'pending_upload',
  is_reference boolean     not null default false,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  constraint images_status_check check (
    status in ('pending_upload','uploaded','processing','done','failed')
  )
);

create unique index uq_images_one_reference_per_project
  on images (project_id) where is_reference = true;

create index ix_images_project_id on images (project_id);
```

Depends on: 002

---

### Migration 004 — add_reference_image_fk_to_projects

Adds the deferred foreign key now that `images` exists. This avoids a circular reference issue in a single migration.

```sql
alter table projects
  add constraint fk_projects_reference_image
  foreign key (reference_image_id) references images(id) on delete set null;
```

Depends on: 003

Rationale: The circular reference (`projects.reference_image_id → images.id` and `images.project_id → projects.id`) cannot be resolved in a single CREATE TABLE statement. Splitting into two migrations is the standard PostgreSQL approach.

---

### Migration 005 — create_template_fields

```sql
create table template_fields (
  id                   uuid        primary key default gen_random_uuid(),
  project_id           uuid        not null references projects(id) on delete cascade,
  field_name           text        not null,
  display_order        int         not null default 0,
  reference_box        jsonb       not null,
  semantic_description text,
  expected_format      jsonb,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  constraint uq_template_fields_name_per_project unique (project_id, field_name),
  constraint template_fields_field_name_nonempty check (char_length(field_name) > 0)
);

create index ix_template_fields_project_id on template_fields (project_id);
```

Depends on: 004

---

### Migration 006 — create_jobs

```sql
create table jobs (
  id              uuid        primary key default gen_random_uuid(),
  project_id      uuid        not null references projects(id) on delete cascade,
  kind            text        not null default 'process_batch',
  status          text        not null default 'queued',
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
  paused_until    timestamptz,
  locked_by       text,
  constraint jobs_status_check check (
    status in ('queued','running','succeeded','failed','cancelled')
  )
);

create unique index uq_jobs_one_active_per_project
  on jobs (project_id) where status in ('queued','running');

create index ix_jobs_status_created on jobs (status, created_at)
  where status = 'queued';
```

Depends on: 005

---

### Migration 007 — create_image_tasks

```sql
create table image_tasks (
  id         uuid        primary key default gen_random_uuid(),
  job_id     uuid        not null references jobs(id) on delete cascade,
  image_id   uuid        not null references images(id) on delete cascade,
  status     text        not null default 'pending',
  error      text,
  updated_at timestamptz not null default now(),
  constraint uq_image_tasks_job_image unique (job_id, image_id),
  constraint image_tasks_status_check check (
    status in ('pending','detecting','extracting','done','failed','not_found')
  )
);

create index ix_image_tasks_job_id on image_tasks (job_id);
```

Depends on: 006

---

### Migration 008 — create_cells

```sql
create table cells (
  id                  uuid         primary key default gen_random_uuid(),
  job_id              uuid         not null references jobs(id) on delete cascade,
  project_id          uuid         not null references projects(id) on delete cascade,
  image_id            uuid         not null references images(id) on delete cascade,
  field_id            uuid         not null references template_fields(id) on delete cascade,
  raw_text            text,
  parsed_value        jsonb,
  unit_seen           text,
  legible             boolean,
  box_confidence      numeric(4,3) not null default 0,
  value_confidence    numeric(4,3) not null default 0,
  combined_confidence numeric(4,3),
  status              text         not null default 'ok',
  failure_reason      text,
  validation_error    text,
  detected_box        jsonb,
  crop_path           text,
  model_versions      jsonb        not null default '{}',
  retried             boolean      not null default false,
  created_at          timestamptz  not null default now(),
  updated_at          timestamptz  not null default now(),
  constraint uq_cells_job_image_field unique (job_id, image_id, field_id),
  constraint cells_status_check check (
    status in ('ok','low_confidence','needs_review','failed')
  ),
  constraint cells_box_confidence_range check (box_confidence between 0 and 1),
  constraint cells_value_confidence_range check (value_confidence between 0 and 1)
);

create index ix_cells_job_id on cells (job_id);
create index ix_cells_project_id on cells (project_id);
create index ix_cells_image_id on cells (image_id);
create index ix_cells_job_status on cells (job_id, status)
  where status in ('low_confidence','needs_review','failed');
```

Depends on: 007

---

### Migration 009 — create_cell_overrides_and_quota

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

create table workspace_quota (
  workspace_id  uuid   primary key references workspaces(id) on delete cascade,
  jobs_today    int    not null default 0,
  images_today  int    not null default 0,
  tokens_today  bigint not null default 0,
  quota_date    date   not null default current_date
);
```

Depends on: 008

Rationale: `cell_overrides` is separate from `cells` so user corrections survive job re-runs. `workspace_quota` enforces per-workspace daily limits checked at enqueue time.

---

## Migration Execution

Migrations run at FastAPI startup before the ASGI app accepts requests:

```
alembic upgrade head
```

This is safe to run repeatedly (idempotent for already-applied migrations). In a Fly.io deploy, run this as a release command before the new instance receives traffic.

---

## Autogenerate Limitations to Know

1. Autogenerate does not detect CHECK constraints reliably — write those by hand.
2. Autogenerate does not detect partial indexes (WHERE clauses) — write those by hand.
3. Always review the autogenerated diff before applying. Autogenerate is a starting point, not the final word.

---

## Future Migration Notes (post-MVP, flagged for later)

- Add `user_id` to `workspaces` when auth is introduced (non-destructive additive column).
- Add `min_value` / `max_value` columns to `template_fields.expected_format` if range validation is needed (currently JSONB, so no migration needed for that field specifically).
- Add `project_id` index to `cells` if cross-job queries become common (e.g., job history comparison view).
- Add `updated_at` triggers (via `pg_cron` or application-level) if stale-read detection is needed.
