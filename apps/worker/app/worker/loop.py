import asyncio
import os
import socket
import asyncpg
import structlog

from app.db import get_pool
from app.worker.handlers import dispatch

log = structlog.get_logger()

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
POLL_INTERVAL = 1.0       # seconds between polls when idle
HEARTBEAT_INTERVAL = 5.0  # seconds between heartbeat updates
STALE_THRESHOLD = 180     # seconds before a running job is considered stale

_kick_event = asyncio.Event()


def kick() -> None:
    """Wake the worker loop immediately instead of waiting for next poll."""
    _kick_event.set()


async def _recover_stale_jobs(pool: asyncpg.Pool) -> None:
    """On startup: reset jobs this instance was running before a crash."""
    await pool.execute(
        """
        UPDATE jobs SET status = 'queued', locked_by = NULL
        WHERE status = 'running' AND locked_by = $1
        """,
        WORKER_ID,
    )


async def _claim_job(pool: asyncpg.Pool) -> dict | None:
    """Claim the oldest queued job, or reclaim a stale running job."""
    row = await pool.fetchrow(
        """
        UPDATE jobs
        SET status       = 'running',
            attempts     = attempts + 1,
            started_at   = COALESCE(started_at, now()),
            heartbeat_at = now(),
            locked_by    = $1
        WHERE id = (
            SELECT id FROM jobs
            WHERE status = 'queued'
               OR (status = 'running'
                   AND heartbeat_at < now() - ($2 || ' seconds')::interval)
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """,
        WORKER_ID,
        str(STALE_THRESHOLD),
    )
    return dict(row) if row else None


async def _heartbeat(pool: asyncpg.Pool, job_id: str) -> None:
    await pool.execute(
        "UPDATE jobs SET heartbeat_at = now() WHERE id = $1", job_id
    )


async def worker_loop() -> None:
    pool = await get_pool()
    await _recover_stale_jobs(pool)
    log.info("worker.started", worker_id=WORKER_ID)

    while True:
        job = await _claim_job(pool)

        if not job:
            try:
                await asyncio.wait_for(_kick_event.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
            finally:
                _kick_event.clear()
            continue

        log.info("worker.job_claimed", job_id=str(job["id"]), kind=job["kind"])

        heartbeat_task = asyncio.create_task(_run_heartbeat(pool, str(job["id"])))
        try:
            await dispatch(pool, job)
            await pool.execute(
                "UPDATE jobs SET status = 'succeeded', finished_at = now() WHERE id = $1",
                job["id"],
            )
            log.info("worker.job_succeeded", job_id=str(job["id"]))
        except Exception as exc:
            log.error("worker.job_failed", job_id=str(job["id"]), error=str(exc))
            if job["attempts"] >= job["max_attempts"]:
                await pool.execute(
                    "UPDATE jobs SET status = 'failed', error = $1, finished_at = now() WHERE id = $2",
                    str(exc),
                    job["id"],
                )
            else:
                await pool.execute(
                    "UPDATE jobs SET status = 'queued', locked_by = NULL, error = $1 WHERE id = $2",
                    str(exc),
                    job["id"],
                )
        finally:
            heartbeat_task.cancel()


async def _run_heartbeat(pool, job_id: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        await _heartbeat(pool, job_id)
