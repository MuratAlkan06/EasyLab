"""Unit tests for the worker loop supervisor behavior.

Before the supervisor fix, a transient asyncpg error in ``_claim_job`` would
propagate out of ``worker_loop``, kill the asyncio task silently, and leave
the process alive but with no claim-loop running (the "stuck pending" wedge).
These tests pin the new behavior: claim failures are logged and the loop
keeps trying.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.worker import loop as loop_module


class _StubPool:
    """Minimal stub for an asyncpg.Pool.

    Implements only the methods the worker loop calls on it. ``fetchrow``
    is wired through to ``self.fetchrow_impl`` so each test can swap in its
    own behavior (raise once, return None, etc.) without monkeypatching
    asyncpg internals.
    """

    def __init__(self):
        self.execute_calls: list[tuple] = []
        self.fetchrow_impl = AsyncMock(return_value=None)

    async def execute(self, *args, **kwargs):
        self.execute_calls.append(args)
        return "UPDATE 0"

    async def fetchrow(self, *args, **kwargs):
        return await self.fetchrow_impl(*args, **kwargs)


async def _run_loop_briefly(pool: _StubPool, *, ticks: float = 0.05) -> None:
    """Spawn worker_loop as a task, let it run a few iterations, then cancel.

    Cancellation is the expected exit path — the loop is ``while True``.
    Anything other than CancelledError surfacing means the supervisor
    swallowed something it shouldn't have, or a different bug fired.
    """
    task = asyncio.create_task(loop_module.worker_loop())
    try:
        await asyncio.sleep(ticks)
    finally:
        task.cancel()
        # The cancellation should propagate cleanly. If the body raised before
        # we cancelled, ``await task`` will re-raise that exception here.
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.fixture(autouse=True)
def _patch_pool_and_speed(monkeypatch: pytest.MonkeyPatch) -> _StubPool:
    """Swap ``get_pool`` for one that returns a stub, and shrink the poll
    interval so tests don't sleep for a real second between ticks. The
    module-level ``_kick_event`` is also replaced with a fresh Event so
    pytest-asyncio's per-test event loops don't trip the cross-loop
    binding that asyncio.Event enforces in Python 3.10+."""
    pool = _StubPool()

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(loop_module, "get_pool", fake_get_pool)
    monkeypatch.setattr(loop_module, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(loop_module, "_kick_event", asyncio.Event())
    return pool


@pytest.mark.asyncio
async def test_claim_failure_does_not_kill_the_task(_patch_pool_and_speed: _StubPool) -> None:
    """A transient asyncpg error during claim must be logged and retried,
    not crash the loop. Before the fix, this exception would propagate out
    of worker_loop and silently kill the task."""
    pool = _patch_pool_and_speed
    pool.fetchrow_impl = AsyncMock(side_effect=RuntimeError("simulated DB blip"))

    await _run_loop_briefly(pool, ticks=0.05)

    # If the supervisor is working, fetchrow has been called multiple times
    # (each retry after the simulated blip).
    assert pool.fetchrow_impl.await_count >= 2, (
        f"expected the loop to retry after a claim failure, but fetchrow "
        f"was only called {pool.fetchrow_impl.await_count} time(s)"
    )


@pytest.mark.asyncio
async def test_no_jobs_available_keeps_polling(_patch_pool_and_speed: _StubPool) -> None:
    """When the claim returns None (no work), the loop should keep polling
    without busy-spinning or crashing."""
    pool = _patch_pool_and_speed
    pool.fetchrow_impl = AsyncMock(return_value=None)

    await _run_loop_briefly(pool, ticks=0.05)

    assert pool.fetchrow_impl.await_count >= 2


@pytest.mark.asyncio
async def test_claim_recovers_after_transient_failure(
    _patch_pool_and_speed: _StubPool,
) -> None:
    """Pool raises once, then returns None ever after. The loop should
    survive the raise and reach the steady-state polling regime."""
    pool = _patch_pool_and_speed
    call_count = 0

    async def flaky_fetchrow(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("first call fails")
        return None

    pool.fetchrow_impl = AsyncMock(side_effect=flaky_fetchrow)

    await _run_loop_briefly(pool, ticks=0.05)

    # Several polls after the failed first one — the loop didn't die.
    assert call_count >= 3
