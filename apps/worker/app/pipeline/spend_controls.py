"""Spend controls for the Gemini pipeline.

Two cost-protection mechanisms live here:

- **Process-wide circuit breaker.** Tracks consecutive 429/5xx failures across
  Gemini calls. When the threshold is hit, callers stamp `jobs.paused_until` so
  the worker loop skips the job for a cooldown window. The breaker is a plain
  module-level counter — the worker is a single process, so we don't need
  inter-process coordination yet.

- **Token accounting.** After every Gemini response we add
  `usage_metadata.total_token_count` to `workspace_quota.tokens_today`. Before a
  job starts we sum `tokens_today` across all workspaces (cheap — bounded row
  count) and refuse to start the job if we're already over the
  `GLOBAL_DAILY_TOKEN_BUDGET` for the day.

Neither mechanism is hard-aborted: in-flight calls keep going. The goal is to
prevent runaway spend, not to roll back work already done.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import asyncpg
import structlog

from app.settings import settings

log = structlog.get_logger()

# Status codes that count toward the breaker. Everything else (validation
# errors, malformed JSON, etc.) is *our* bug, not Gemini's, and shouldn't pause
# the worker.
_BREAKER_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass
class _BreakerState:
    consecutive_failures: int = 0


_state = _BreakerState()


def _exception_status(exc: BaseException) -> int | None:
    """Best-effort extraction of the HTTP status from a google-genai exception.

    The google-genai SDK exposes `.code` as the HTTP status on its `APIError`
    hierarchy. Other libraries (httpx, etc.) carry a `status_code` attribute.
    Anything we can't read returns None so the caller can decide whether to
    count it.
    """
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def record_failure(exc: BaseException) -> bool:
    """Record a Gemini call failure. Returns True if the breaker has tripped.

    Only 429s and 5xx count. A tripped breaker means the caller should stamp
    `paused_until` on its job and stop calling Gemini for the cooldown window.
    """
    status = _exception_status(exc)
    if status not in _BREAKER_STATUS_CODES:
        return False

    _state.consecutive_failures += 1
    tripped = _state.consecutive_failures >= settings.gemini_breaker_threshold
    log.warning(
        "spend_controls.gemini_failure",
        status=status,
        consecutive_failures=_state.consecutive_failures,
        threshold=settings.gemini_breaker_threshold,
        tripped=tripped,
    )
    return tripped


def record_success() -> None:
    """Record a Gemini call success — resets the breaker counter."""
    if _state.consecutive_failures > 0:
        log.info(
            "spend_controls.gemini_recovered",
            previous_consecutive_failures=_state.consecutive_failures,
        )
    _state.consecutive_failures = 0


def consecutive_failures() -> int:
    """Read-only view of the current breaker counter (exposed for tests)."""
    return _state.consecutive_failures


def reset_state() -> None:
    """Reset breaker state to zero. Test-only helper."""
    _state.consecutive_failures = 0


def extract_total_tokens(response: object) -> int:
    """Pull total_token_count out of a Gemini response, defensively.

    Returns 0 when the SDK gave us a response without usage_metadata — better to
    under-count than to throw inside the worker over a token budget detail.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0
    total = getattr(usage, "total_token_count", None)
    if not isinstance(total, int) or total < 0:
        return 0
    return total


async def record_token_usage(
    pool: asyncpg.Pool,
    workspace_id,
    total_tokens: int,
) -> None:
    """Add `total_tokens` to workspace_quota.tokens_today (UPSERT, daily rollover).

    Best-effort: logs on failure but does not raise — token accounting must not
    block the pipeline.
    """
    if total_tokens <= 0:
        return
    today = date.today()
    try:
        await pool.execute(
            """
            INSERT INTO workspace_quota (workspace_id, tokens_today, quota_date)
            VALUES ($1, $2, $3::date)
            ON CONFLICT (workspace_id) DO UPDATE
            SET tokens_today = CASE
                  WHEN workspace_quota.quota_date < $3::date THEN EXCLUDED.tokens_today
                  ELSE workspace_quota.tokens_today + EXCLUDED.tokens_today
                END,
                quota_date = $3::date
            """,
            workspace_id,
            total_tokens,
            today,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "spend_controls.record_token_usage_failed",
            workspace_id=str(workspace_id),
            tokens=total_tokens,
            error=str(exc),
        )


async def global_tokens_today(pool: asyncpg.Pool) -> int:
    """Sum tokens_today across all workspaces whose quota_date is today."""
    today = date.today()
    row = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(tokens_today), 0)::bigint AS total
        FROM workspace_quota
        WHERE quota_date = $1::date
        """,
        today,
    )
    return int(row["total"]) if row else 0


async def workspace_tokens_today(pool: asyncpg.Pool, workspace_id) -> int:
    """Read tokens_today for a single workspace. Returns 0 if no row exists or
    the cached row is for a previous day (rollover happens lazily on the next
    write)."""
    today = date.today()
    row = await pool.fetchrow(
        """
        SELECT tokens_today
        FROM workspace_quota
        WHERE workspace_id = $1 AND quota_date = $2::date
        """,
        workspace_id,
        today,
    )
    return int(row["tokens_today"]) if row else 0


async def assert_global_budget_available(pool: asyncpg.Pool) -> None:
    """Raise if the global daily token budget is already exhausted.

    Soft check: jobs that have already started keep running. We just refuse to
    start new ones once the budget is gone.
    """
    used = await global_tokens_today(pool)
    if used >= settings.global_daily_token_budget:
        raise BudgetExceeded(
            f"Global daily token budget exhausted "
            f"({used:,} of {settings.global_daily_token_budget:,} used)."
        )


async def assert_workspace_budget_available(
    pool: asyncpg.Pool, workspace_id
) -> None:
    """Raise if this workspace has already burned through its daily token cap.

    Same soft-check semantics as the global budget — refuses new jobs, lets
    in-flight ones finish."""
    if workspace_id is None:
        return
    cap = settings.workspace_daily_token_budget
    if cap <= 0:
        return
    used = await workspace_tokens_today(pool, workspace_id)
    if used >= cap:
        raise BudgetExceeded(
            f"Workspace daily token budget exhausted "
            f"({used:,} of {cap:,} used)."
        )


class BudgetExceeded(Exception):
    """Raised when the global daily token budget is exhausted."""


async def pause_job(pool: asyncpg.Pool, job_id, seconds: int | None = None) -> None:
    """Stamp `paused_until = now() + seconds` on the job and clear the lock.

    Used when the breaker trips. The worker loop's claim query already skips
    jobs whose paused_until > now() (added by this PR).
    """
    cooldown = seconds if seconds is not None else settings.gemini_breaker_cooldown_seconds
    await pool.execute(
        """
        UPDATE jobs
        SET status       = 'queued',
            locked_by    = NULL,
            paused_until = now() + ($1 || ' seconds')::interval
        WHERE id = $2
        """,
        str(cooldown),
        job_id,
    )
    log.warning(
        "spend_controls.job_paused",
        job_id=str(job_id),
        cooldown_seconds=cooldown,
    )
