"""Fake job handler for Phase 1 topology smoke test.

Simulates work by sleeping and incrementing progress_done 0 → 5.
Kill the worker mid-run and restart to verify crash recovery.
"""

import asyncio

import asyncpg
import structlog

log = structlog.get_logger()

STEPS = 5


async def handle_fake(pool: asyncpg.Pool, job: dict) -> None:
    job_id = job["id"]
    await pool.execute("UPDATE jobs SET progress_total = $1 WHERE id = $2", STEPS, job_id)

    for i in range(1, STEPS + 1):
        await asyncio.sleep(1)
        await pool.execute(
            "UPDATE jobs SET progress_done = $1, heartbeat_at = now() WHERE id = $2",
            i,
            job_id,
        )
        log.info("fake_job.step", job_id=str(job_id), step=i, total=STEPS)
