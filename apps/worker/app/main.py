import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.internal import router as internal_router
from app.db import close_pool, get_pool
from app.settings import settings
from app.worker.loop import worker_loop

log = structlog.get_logger()

_worker_tasks: list[asyncio.Task] = []


def _run_migrations() -> None:
    from alembic.config import Config as AlembicConfig

    from alembic import command as alembic_command

    cfg = AlembicConfig("alembic.ini")
    alembic_command.upgrade(cfg, "head")


def _on_worker_task_done(task: asyncio.Task) -> None:
    """Log if a worker_loop task ever exits.

    The loop's claim block already retries on transient errors, but if a
    bug ever escapes it the task would die silently. asyncio.create_task
    drops unhandled exceptions on the floor unless you add a done callback,
    which is what was causing the 'process alive but no claim-loop'
    wedge before loop.py was hardened.
    """
    if task.cancelled():
        return  # cooperative shutdown — normal
    exc = task.exception()
    if exc is not None:
        log.error(
            "worker.task_crashed",
            task_name=task.get_name(),
            error=str(exc),
            error_type=type(exc).__name__,
        )
    else:
        log.warning("worker.task_exited_clean", task_name=task.get_name())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — run DB migrations before accepting traffic
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _run_migrations)
    await get_pool()
    for i in range(settings.worker_concurrency):
        task = asyncio.create_task(worker_loop(), name=f"worker-{i}")
        task.add_done_callback(_on_worker_task_done)
        _worker_tasks.append(task)
    log.info("app.started", workers=settings.worker_concurrency)

    yield

    # Shutdown
    for task in _worker_tasks:
        task.cancel()
    await asyncio.gather(*_worker_tasks, return_exceptions=True)
    await close_pool()
    log.info("app.stopped")


app = FastAPI(title="EasyLab Worker", lifespan=lifespan)
app.include_router(health_router)
app.include_router(internal_router)
