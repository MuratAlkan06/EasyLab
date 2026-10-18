# EasyLab

[![CI](https://github.com/MuratAlkan06/EasyLab/actions/workflows/ci.yml/badge.svg)](https://github.com/MuratAlkan06/EasyLab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## What is EasyLab?

EasyLab turns batches of repetitive lab photos — meter readings, instrument displays, test strips, sample labels — into structured, exportable data without writing a script for every device.

Manually transcribing values off dozens of near-identical images is slow and error-prone, but every batch looks slightly different, so off-the-shelf OCR misses fields or grabs the wrong ones. EasyLab splits the work: **you** show it what to look for on one reference image by drawing boxes around the fields that matter; **the AI** learns what each region represents and then finds and reads the same fields on every other image in the batch. You review the results in a table (with crop thumbnails next to each value so you can trust what was extracted), fix anything that looks off, and export to CSV.

**Who it's for:** lab technicians, researchers, QA teams, and anyone who needs to pull the same handful of values off a stack of similar images and would rather not type them in by hand.

> **Core idea:** Label one image once → AI learns what each region means → AI finds and extracts the same fields across the rest.

---

## Architecture

![Infrastructure diagram](docs/diagrams/infrastructure.png)

Two services + Supabase.

- **`apps/web`** — Next.js 15 on Vercel. Owns UI, signed upload URLs, project/image/field CRUD, job enqueue, CSV export.
- **`apps/worker`** — FastAPI on Fly.io (persistent VM). Owns all Gemini calls, all image processing (Pillow, OpenCV, PaddleOCR), and the in-process asyncio job queue.
- **Supabase** — Postgres for data, Storage for image files (private bucket `easylab`).

The browser uploads files **directly** to Supabase Storage via a signed URL minted by Next.js — uploads never pass through the API. Next.js never calls Gemini; all AI runs on the FastAPI worker.

More diagrams (data flow, ERD, state machines) live in [`docs/diagrams/`](docs/diagrams/index.html).

---

## Repo layout

```
.
├── apps/
│   ├── web/            Next.js 15 (TypeScript, Tailwind, react-konva, shadcn/ui)
│   └── worker/         FastAPI + asyncio (Pydantic v2, asyncpg, Pillow, OpenCV, PaddleOCR)
├── docs/
│   ├── api-contract.md
│   ├── ai-pipeline.md
│   ├── database-schema.md
│   ├── migrations-strategy.md
│   ├── ui-spec.md
│   └── diagrams/       HTML architecture diagrams
├── docker-compose.yml  Local dev (web + worker)
├── CLAUDE.md           Project instructions for Claude Code
└── .env.example        All env vars (Supabase, Gemini, internal secret)
```

---

## Local setup

**Prerequisites:** Node 20+, pnpm 10+, Python 3.13+, Docker, a Supabase project, a Gemini API key.

1. **Clone and install JS deps**
   ```bash
   pnpm install
   ```

2. **Create env files**
   ```bash
   cp .env.example apps/worker/.env
   cp .env.example apps/web/.env.local   # then trim to only the NEXT_* + Supabase vars
   ```
   Fill in: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `DATABASE_URL`, `GEMINI_API_KEY`, `INTERNAL_SHARED_SECRET` (`openssl rand -hex 32`).

3. **Run database migrations** (from `apps/worker/`)
   ```bash
   cd apps/worker
   alembic upgrade head
   ```

4. **Start both services**
   ```bash
   docker compose up
   ```
   - Web → http://localhost:3000
   - Worker → http://localhost:8000

   Or run them separately without Docker:
   ```bash
   pnpm dev:web         # Next.js
   pnpm dev:worker      # FastAPI (uvicorn)
   ```

---

## Stack

| Layer | Tools |
|---|---|
| Frontend | Next.js 15, TypeScript, Tailwind, react-konva, shadcn/ui, TanStack Table/Virtual/Query, Zod |
| Backend | FastAPI, Pydantic v2, asyncpg, supabase-py, Pillow, OpenCV, tenacity, structlog |
| AI | Gemini 2.5 Pro (template gen), Gemini 2.5 Flash (extraction), PaddleOCR (parallel reader) |
| Detection | Classical CV aligner (OpenCV: AKAZE + seeded RANSAC homography + NCC verification), no model call |
| Data | Supabase Postgres + Storage, Alembic migrations |
| Export | CSV (XLSX deferred) |

### AI pipeline

![AI pipeline diagram](docs/diagrams/ai-pipeline.png)

Three stages run on the FastAPI worker. Stage A enriches user annotations into a semantic template (once per project). Stage B finds each field on every other image without a model call. It aligns the image to the reference with OpenCV (AKAZE features + seeded RANSAC homography, sanity-gated), carries the user's reference boxes across, and verifies them with template matching. When alignment is unsure it falls back to the reference box at lower confidence. Stage C reads the value from each crop with Gemini Flash and PaddleOCR running in parallel for cross-validation. Every Gemini call uses native JSON schema from Pydantic — no prompt-engineered JSON. See [`docs/ai-pipeline.md`](docs/ai-pipeline.md) for full details.

**Detection accuracy** comes from the offline eval in [`apps/worker/eval/`](apps/worker/eval/): 12 bench-crop targets, 48 field instances, hit = IoU ≥ 0.5.

| Stage B detector | Mean IoU | Hit-rate | Cost / determinism |
|---|---|---|---|
| Gemini 2.5 Pro (before Phase 7) | 0.389 | 38% | 1 Pro call per image, stochastic |
| Deterministic aligner (Phase 7) | 0.830 | 96% | $0, bit-identical across runs |

---

## Build phases

Phases 1–6 shipped in [`v0.6.0`](https://github.com/MuratAlkan06/EasyLab/releases/tag/v0.6.0) (2026-05-13); Phase 7 followed in 2026-10.

- [x] **Phase 1** — Monorepo scaffold + docker-compose + Supabase migrations + fake job smoke test
- [x] **Phase 2** — Template generation (Gemini 2.5 Pro adds `semantic_description`)
- [x] **Phase 3** — Full detection + extraction (review table populates)
- [x] **Phase 4** — Crops + confidence + `needs_review` flags
- [x] **Phase 5** — CSV export + `cell_overrides` survive re-runs
- [x] **Phase 6** — Polish: circuit breaker, global token budget, per-workspace daily image quota, server-trusted size + mime, byte-decoded width/height, per-workspace token cap, quota UI, Supabase Realtime, vitest harness
- [x] **Phase 7** — Deterministic Stage B detection: Gemini detection replaced by a classical CV aligner, EXIF upright-space canonicalization, offline detection eval harness

---

## Key constraints

- Anonymous workspace per session (HttpOnly signed UUID cookie — no auth in v1)
- Desktop only (≥1280px), JPG/PNG ≤20 MB, 10–50 images, max 20 fields per project
- All bounding boxes stored normalized 0..1, never display pixels
- All Gemini calls use native `response_json_schema` from Pydantic models — no prompt-engineered JSON
- Service-role key is server-side only — never `NEXT_PUBLIC_*`
- Spend controls (per-workspace daily quota + global token budget) gate every job

---

## Documentation

| Doc | When to read |
|---|---|
| [`docs/database-schema.md`](docs/database-schema.md) | Writing migrations, adding columns, understanding state machines |
| [`docs/api-contract.md`](docs/api-contract.md) | Implementing any API route |
| [`docs/migrations-strategy.md`](docs/migrations-strategy.md) | Before running Alembic |
| [`docs/ai-pipeline.md`](docs/ai-pipeline.md) | Implementing any Gemini / Stage B alignment / OCR / confidence logic |
| [`docs/adr-007-stage-b-detection.md`](docs/adr-007-stage-b-detection.md) | Why Stage B is a deterministic aligner: options considered, engineering rules, eval results, rollback |
| [`apps/worker/eval/README.md`](apps/worker/eval/README.md) | Before changing Stage B detection (offline accuracy eval) |
| [`docs/ui-spec.md`](docs/ui-spec.md) | Implementing any frontend page or canvas interaction |
| [`docs/diagrams/index.html`](docs/diagrams/index.html) | Visual reference — infrastructure, data flow, AI pipeline, ERD, state machines |
