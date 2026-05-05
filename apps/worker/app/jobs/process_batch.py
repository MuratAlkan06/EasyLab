"""Batch processing handler — implemented in Phase 3+."""

import asyncpg


async def handle_process_batch(pool: asyncpg.Pool, job: dict) -> None:
    raise NotImplementedError("process_batch implemented in Phase 3")
