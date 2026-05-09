"""Batch processing handler.

Phase 2 implements Stage A only (template generation). After Stage A the
project status returns to 'annotated' to signal "template ready, detection not
yet run". Phase 3+ extends this to run Stages B and C and move the project to
'done' when extraction completes.
"""

import asyncpg
import structlog

from app.pipeline.stage_a import run_stage_a

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

    # Phase 2: project returns to 'annotated' once the template is generated.
    # Phase 3+ will move it to 'done' after Stages B and C complete.
    await pool.execute(
        "UPDATE projects SET status = 'annotated', updated_at = now() WHERE id = $1",
        project_id,
    )

    await pool.execute(
        """
        UPDATE jobs
        SET progress_total = 1,
            progress_done  = 1,
            heartbeat_at   = now()
        WHERE id = $1
        """,
        job_id,
    )

    log.info("process_batch.complete", job_id=str(job_id), project_id=str(project_id))
