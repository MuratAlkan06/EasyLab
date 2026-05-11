"""Batch processing handler.

Runs Stage A (template generation), Stage B (region detection), and Stage C
(value extraction). On success, flips both the project and the job to their
terminal ``done``/``succeeded`` states.
"""

import asyncpg
import structlog

from app.pipeline.stage_a import run_stage_a
from app.pipeline.stage_b import run_stage_b
from app.pipeline.stage_c import run_stage_c

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

    # Stage B — region detection (Gemini 2.5 Pro). Returns a per-image,
    # per-field box map that Stage C consumes.
    detected_boxes = await run_stage_b(pool, job)

    # Stage C — value extraction (Gemini 2.5 Flash). Writes one cells row
    # per detected field and uploads crops to Supabase Storage.
    await run_stage_c(pool, job, detected_boxes)

    # Promote the project to its terminal state and mark the job succeeded.
    # progress_done is bumped to progress_total because Stage B and Stage C
    # advance progress per-image but the final tally is exactly the batch size.
    await pool.execute(
        "UPDATE projects SET status = 'done', updated_at = now() WHERE id = $1",
        project_id,
    )
    await pool.execute(
        """
        UPDATE jobs
        SET status        = 'succeeded',
            finished_at   = now(),
            progress_done = progress_total,
            heartbeat_at  = now()
        WHERE id = $1
        """,
        job_id,
    )

    log.info("process_batch.complete", job_id=str(job_id), project_id=str(project_id))
