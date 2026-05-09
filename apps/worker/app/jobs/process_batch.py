"""Batch processing handler.

Runs Stage A (template generation) and Stage B (region detection). Stage C
(value extraction) is still TODO; until it lands, this handler leaves the
project in ``processing`` and lets the next slice promote it to ``done``.
"""

import asyncpg
import structlog

from app.pipeline.stage_a import run_stage_a
from app.pipeline.stage_b import run_stage_b

log = structlog.get_logger()


async def handle_process_batch(pool: asyncpg.Pool, job: dict) -> None:
    job_id = job["id"]
    project_id = job["project_id"]

    log.info("process_batch.start", job_id=str(job_id), project_id=str(project_id))

    await pool.execute(
        "UPDATE projects SET status = 'processing', updated_at = now() WHERE id = $1",
        project_id,
    )

    # Stage A — template generation (Gemini 2.5 Pro).
    await run_stage_a(pool, job)

    # Stage B — region detection (Gemini 2.5 Pro). Returns a per-image
    # box map that Stage C will consume in a follow-up slice.
    await run_stage_b(pool, job)

    # Stage B advances per-image progress. The job-level heartbeat is
    # bumped here so the worker poll loop doesn't think we're stuck even
    # if Stage B produced zero successful detections.
    await pool.execute(
        "UPDATE jobs SET heartbeat_at = now() WHERE id = $1",
        job_id,
    )

    # TODO(stage-c): once Stage C extraction lands, flip the project to
    # 'done' here. For now we leave it in 'processing' so the UI can show
    # a clear "extraction not yet implemented" state without rolling back
    # to 'annotated'.

    log.info("process_batch.complete", job_id=str(job_id), project_id=str(project_id))
