# EasyLab Project Instructions

EasyLab is a web app where users upload a batch of similar lab images, label regions on one reference image, and AI extracts the same fields across all images into a review table and CSV export.

Core concept: "Label one image once → AI learns what each region means → AI finds and extracts the same fields across the rest."

---

## Current Phase

Phases 1–4 complete on main. Phase 5 + Phase 6 partial in flight as open PRs.
Do not skip phases.

1. ✅ Monorepo scaffold + docker-compose + Supabase migrations + fake job smoke test (no AI)
2. ✅ Template generation — Gemini 2.5 Pro enriches annotations with semantic_description (PR #16)
3. ✅ Full detection + extraction — pipeline complete (Stages B + C + PaddleOCR) (PR #18). Detection accuracy is acknowledged unresolved; see `memory/project_phase3_detection_status.md`. Visual reference-crop matching attempt is stashed.
4. ✅ Crops + confidence + needs_review — crop images shown in review table (landed inside Phase 3 slices, PR #18)
5. 🟡 CSV export + cell_overrides survive re-runs — PR #19 open
6. 🟡 Polish — partial:
   - ✅ CI build step + image quota (`MAX_IMAGES_PER_WORKSPACE_PER_DAY`) (PR #22)
   - 🟡 Server-trusted size + mime on image confirm — PR #23 open
   - 🟡 Gemini circuit breaker + global token budget + worker pytest in CI — PR #24 open
   - 🟡 Vitest scaffold + csv tests — PR #19 open
   - ⏳ Supabase Realtime on processing page (currently polls every 1.5s)
   - ⏳ Per-cell width/height verification on image confirm (needs byte decode)
   - ⏳ Per-workspace token-cap enforcement (`tokens_today` populated, no reader yet)

---

## Stack (Final)

- **Frontend:** Next.js 15, TypeScript, Tailwind CSS, react-konva, shadcn/ui, Sonner, @tanstack/react-table, @tanstack/react-virtual, @tanstack/react-query, Zod
- **Backend:** FastAPI + asyncio on Fly.io (persistent VM — not serverless), Pydantic v2, asyncpg, supabase-py, Pillow, tenacity, structlog
- **AI:** Gemini 2.5 Pro (template gen + detection), Gemini 2.5 Flash (value extraction), PaddleOCR (Stage C parallel reader)
- **Data:** Supabase Postgres + Supabase Storage (private bucket `easylab`), Alembic migrations
- **Export:** CSV only in MVP (XLSX deferred)

---

## Architecture

Two services + Supabase. Next.js on Vercel, FastAPI on Fly.io.

- **Next.js owns:** UI, signed upload URL minting, project/image/field CRUD, job enqueue, CSV export, polling proxy
- **FastAPI owns:** ALL Gemini calls, ALL Pillow/OpenCV ops, asyncio job worker, writing cells/crops/thumbnails
- **Next.js never calls Gemini. FastAPI never serves the UI.**

Job queue: DB-backed in-process asyncio worker. `FOR UPDATE SKIP LOCKED`. No Redis, no Celery.
Storage paths: `projects/{project_id}/originals|thumbs|crops/{image_id}`

---

## MVP Scope

**In v1:**
- Anonymous workspace per session (HttpOnly signed UUIDv4 cookie — no auth)
- Upload 10–50 JPG/PNG, ≤20 MB each
- Reference image selected from uploaded set (inline in upload flow)
- Rectangular annotation (react-konva), max 20 fields
- Semantic template: reference_box + semantic_description per field
- Batch AI detection + extraction with confidence scores + needs_review flags
- Crop images saved and shown next to values in review table (primary trust mechanism)
- Review table: inline editing (double-click or Enter), cell overrides survive re-runs
- CSV export, per-workspace daily quota, global token budget

**Out of v1:**
- Auth, multi-user, payments
- XLSX with thumbnails, visual anchors, min/max range validation
- Per-image/per-cell retry (re-annotate = full restart)
- Mobile layout (desktop only, min 1280px)

---

## Coding Rules

- Use Context7 for any library, API, or SDK documentation — even well-known ones.
- All Gemini calls use native `response_json_schema` via Pydantic `.model_json_schema()`. No prompt-engineered JSON.
- All AI responses validated with Pydantic before any downstream use.
- Never store bounding boxes in display pixels. Always normalize to 0..1 against original image dimensions.
- `combined_confidence` is a regular column written by the worker (never a Postgres generated column).
- `cell_overrides` keyed by `(project_id, image_id, field_id)` — never by `job_id`.
- Service-role key in server-side code only. Never in browser, never in `NEXT_PUBLIC_*`.
- Implement spend controls (workspace_quota table + global token budget) before any Gemini code.
- Do not overengineer. Prefer working end-to-end before polish.
- Do not build auth, payments, or multi-user support unless explicitly approved.
- Treat `docs/` as read-only reference. Do not modify any file in `docs/` unless explicitly asked.

---

## Reference Documents

- `docs/database-schema.md` — open when writing migrations, DB queries, adding columns, or understanding table relationships and state machines
- `docs/api-contract.md` — open when implementing any API route: request/response shape, validation rules, error codes
- `docs/migrations-strategy.md` — open before writing or running any Alembic migration
- `docs/ai-pipeline.md` — open when implementing any Gemini call, PaddleOCR integration, confidence scoring, preprocessing, or circuit breaker logic
- `docs/ui-spec.md` — open when implementing any frontend page, component, canvas interaction, or review table behavior
