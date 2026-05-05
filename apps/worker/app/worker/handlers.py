import asyncpg

from app.jobs.fake import handle_fake
from app.jobs.process_batch import handle_process_batch


async def dispatch(pool: asyncpg.Pool, job: dict) -> None:
    match job["kind"]:
        case "fake":
            await handle_fake(pool, job)
        case "process_batch":
            await handle_process_batch(pool, job)
        case _:
            raise ValueError(f"Unknown job kind: {job['kind']}")
