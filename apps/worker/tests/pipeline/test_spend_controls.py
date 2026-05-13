"""Unit tests for app/pipeline/spend_controls.py.

Covers the circuit breaker (pure in-memory logic) and the token extractor.
DB-touching helpers (record_token_usage / global_tokens_today / pause_job /
assert_global_budget_available) are integration-shaped and live behind a real
asyncpg pool, so they're not exercised here.
"""

from __future__ import annotations

import os

# Settings reads env at import time via pydantic-settings; supply minimal
# placeholders so importing the app modules doesn't blow up during test
# collection.
os.environ.setdefault("SUPABASE_URL", "https://placeholder.local")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "placeholder")
os.environ.setdefault("DATABASE_URL", "postgresql://placeholder/placeholder")
os.environ.setdefault("INTERNAL_SHARED_SECRET", "placeholder")

import pytest  # noqa: E402

from app.pipeline import spend_controls  # noqa: E402
from app.settings import settings  # noqa: E402


@pytest.fixture(autouse=True)
def reset_breaker():
    spend_controls.reset_state()
    yield
    spend_controls.reset_state()


class _Exc429(Exception):
    """Mimic the .code attribute that google-genai's APIError carries."""

    def __init__(self, code: int):
        super().__init__(f"status {code}")
        self.code = code


class _ExcHttpx(Exception):
    """Mimic httpx-style .status_code attribute."""

    def __init__(self, status_code: int):
        super().__init__(f"http {status_code}")
        self.status_code = status_code


def test_record_failure_ignores_non_retryable_status():
    assert spend_controls.record_failure(_Exc429(400)) is False
    assert spend_controls.consecutive_failures() == 0


def test_record_failure_ignores_status_less_message():
    assert spend_controls.record_failure(ValueError("malformed json")) is False
    assert spend_controls.consecutive_failures() == 0


def test_record_failure_counts_429():
    assert spend_controls.record_failure(_Exc429(429)) is False
    assert spend_controls.consecutive_failures() == 1


def test_record_failure_counts_5xx():
    for code in (500, 502, 503, 504):
        spend_controls.reset_state()
        assert spend_controls.record_failure(_Exc429(code)) is False
        assert spend_controls.consecutive_failures() == 1


def test_record_failure_trips_at_threshold():
    threshold = settings.gemini_breaker_threshold
    for i in range(1, threshold):
        tripped = spend_controls.record_failure(_Exc429(503))
        assert tripped is False, f"breaker tripped early on failure #{i}"
    final = spend_controls.record_failure(_Exc429(503))
    assert final is True
    assert spend_controls.consecutive_failures() == threshold


def test_record_success_resets_counter():
    spend_controls.record_failure(_Exc429(429))
    spend_controls.record_failure(_Exc429(503))
    assert spend_controls.consecutive_failures() == 2
    spend_controls.record_success()
    assert spend_controls.consecutive_failures() == 0


def test_record_failure_reads_httpx_status_code_attr():
    assert spend_controls.record_failure(_ExcHttpx(429)) is False
    assert spend_controls.consecutive_failures() == 1


class _UsageMeta:
    def __init__(self, total: int | None):
        self.total_token_count = total


class _Response:
    def __init__(self, usage):
        self.usage_metadata = usage


def test_extract_total_tokens_reads_usage_metadata():
    response = _Response(_UsageMeta(1234))
    assert spend_controls.extract_total_tokens(response) == 1234


def test_extract_total_tokens_returns_zero_when_metadata_missing():
    assert spend_controls.extract_total_tokens(_Response(None)) == 0
    assert spend_controls.extract_total_tokens(object()) == 0


def test_extract_total_tokens_returns_zero_on_negative_or_non_int():
    assert spend_controls.extract_total_tokens(_Response(_UsageMeta(-5))) == 0
    assert spend_controls.extract_total_tokens(_Response(_UsageMeta(None))) == 0
    assert spend_controls.extract_total_tokens(_Response(_UsageMeta("100"))) == 0
