"""Unit tests for Stage C (value extraction) orchestration.

All Gemini calls are mocked. No real network access. No database — the
asyncpg pool is replaced with an ``AsyncMock`` so we can assert on the SQL
calls the orchestrator makes without standing up Postgres. Crop encoding is
mocked too so we don't depend on Pillow's encoder for these assertions.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.pipeline import stage_c
from app.pipeline.schemas import ExtractionResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_pool() -> AsyncMock:
    """Build an async-mock asyncpg pool with the methods Stage C uses."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    return pool


def _make_job() -> dict:
    return {"id": uuid4(), "project_id": uuid4()}


def _make_field(field_name: str, expected_format: dict | None = None) -> dict:
    return {
        "id": uuid4(),
        "field_name": field_name,
        "expected_format": expected_format,
    }


def _make_detection(
    field_id: UUID,
    *,
    box_confidence: float = 0.9,
    box: dict | None = None,
    width: int = 1000,
    height: int = 800,
) -> dict[UUID, dict]:
    return {
        field_id: {
            "box": box or {"x": 0.2, "y": 0.1, "width": 0.2, "height": 0.2},
            "box_confidence": box_confidence,
            "image_width": width,
            "image_height": height,
        }
    }


def _cell_insert_calls(pool: AsyncMock) -> list[Any]:
    """All execute calls that hit the cells INSERT/UPSERT statement."""
    return [c for c in pool.execute.await_args_list if "INSERT INTO cells" in c.args[0]]


def _cell_kwargs(call_args) -> dict:
    """Extract positional args from a cells INSERT call into a named dict.

    Mirrors the parameter order in ``stage_c._write_cell``:
    ``(sql, job_id, project_id, image_id, field_id, raw_text, parsed_value,
       unit_seen, legible, box_confidence, value_confidence,
       combined_confidence, status, failure_reason, validation_error,
       detected_box, crop_path, model_versions)``.
    """
    a = call_args.args
    return {
        "job_id": a[1],
        "project_id": a[2],
        "image_id": a[3],
        "field_id": a[4],
        "raw_text": a[5],
        "parsed_value": a[6],
        "unit_seen": a[7],
        "legible": a[8],
        "box_confidence": a[9],
        "value_confidence": a[10],
        "combined_confidence": a[11],
        "status": a[12],
        "failure_reason": a[13],
        "validation_error": a[14],
        "detected_box": a[15],
        "crop_path": a[16],
        "model_versions": a[17],
    }


async def _run_process_image(
    *,
    field: dict,
    extraction: ExtractionResponse | None = None,
    extraction_raises: Exception | None = None,
    upload_raises: Exception | None = None,
    detection_overrides: dict | None = None,
    ocr_text: str | None = None,
) -> tuple[AsyncMock, dict]:
    """Drive ``stage_c._process_image`` with a single field/detection.

    ``ocr_text`` is the value the (mocked) PaddleOCR reader returns. Default
    ``None`` exercises the OCR-failed/empty reconciliation path (row 3).
    """
    pool = _stub_pool()
    job = _make_job()
    image_id = uuid4()

    # Single-field detection map.
    detection_kwargs: dict[str, Any] = {}
    if detection_overrides:
        detection_kwargs.update(detection_overrides)
    detections = _make_detection(field["id"], **detection_kwargs)

    fields_by_id = {field["id"]: field}
    image_task = {
        "task_id": uuid4(),
        "image_id": image_id,
        "storage_path": "projects/x/originals/y.jpg",
    }

    fake_image = MagicMock()
    fake_image.close = MagicMock()

    if extraction_raises is not None:
        call_with_retry = AsyncMock(side_effect=extraction_raises)
    else:
        call_with_retry = AsyncMock(return_value=extraction)

    upload_mock = MagicMock()
    if upload_raises is not None:
        upload_mock.side_effect = upload_raises

    with (
        patch.object(stage_c, "_download_image_bytes", return_value=b"raw"),
        patch.object(stage_c, "_open_image", return_value=fake_image),
        patch.object(stage_c, "crop_and_encode", return_value=b"crop-bytes"),
        patch.object(stage_c, "_upload_crop_bytes", new=upload_mock),
        patch.object(stage_c, "_call_with_retry", new=call_with_retry),
        patch.object(stage_c, "run_ocr", return_value=ocr_text),
    ):
        await stage_c._process_image(
            pool,
            job=job,
            image_id=image_id,
            detections=detections,
            fields_by_id=fields_by_id,
            image_task=image_task,
            client=object(),
            progress_lock=asyncio.Lock(),
        )

    return pool, {
        "job_id": job["id"],
        "project_id": job["project_id"],
        "image_id": image_id,
        "field_id": field["id"],
        "task_id": image_task["task_id"],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extraction_writes_cell_ok() -> None:
    """High-confidence legible numeric extraction → status='ok' cell written."""
    field = _make_field("Voltage", expected_format={"type": "number", "unit": "V"})
    extraction = ExtractionResponse(
        raw_text="12.34 V",
        parsed_value=12.34,
        unit_seen="V",
        legible=True,
        self_confidence=0.95,
    )

    pool, ids = await _run_process_image(
        field=field,
        extraction=extraction,
        detection_overrides={"box_confidence": 0.9},
    )

    inserts = _cell_insert_calls(pool)
    assert len(inserts) == 1
    row = _cell_kwargs(inserts[0])
    assert row["status"] == "ok"
    assert row["validation_error"] is None
    assert row["legible"] is True
    assert row["raw_text"] == "12.34 V"
    assert row["unit_seen"] == "V"
    # parsed_value is JSON-serialized for jsonb.
    assert json.loads(row["parsed_value"]) == 12.34
    assert row["crop_path"] is not None
    assert row["failure_reason"] is None


@pytest.mark.asyncio
async def test_extraction_legible_false_writes_needs_review() -> None:
    """legible=False forces status='needs_review' regardless of confidence."""
    field = _make_field("Voltage")
    extraction = ExtractionResponse(
        raw_text="??",
        parsed_value="??",
        unit_seen=None,
        legible=False,
        self_confidence=0.95,
    )

    pool, _ = await _run_process_image(field=field, extraction=extraction)

    row = _cell_kwargs(_cell_insert_calls(pool)[0])
    assert row["status"] == "needs_review"
    assert row["legible"] is False


@pytest.mark.asyncio
async def test_extraction_validation_error_number() -> None:
    """expected_format type=number but parsed_value is a string → validation_error set."""
    field = _make_field("Voltage", expected_format={"type": "number", "unit": None})
    extraction = ExtractionResponse(
        raw_text="abc",
        parsed_value="abc",
        unit_seen=None,
        legible=True,
        self_confidence=0.95,
    )

    pool, _ = await _run_process_image(field=field, extraction=extraction)

    row = _cell_kwargs(_cell_insert_calls(pool)[0])
    assert row["validation_error"] == "expected number, got string"
    assert row["status"] == "needs_review"


@pytest.mark.asyncio
async def test_extraction_validation_error_unit() -> None:
    """expected unit V but unit_seen mA → validation_error set, status needs_review."""
    field = _make_field("Voltage", expected_format={"type": "number", "unit": "V"})
    extraction = ExtractionResponse(
        raw_text="50 mA",
        parsed_value=50,
        unit_seen="mA",
        legible=True,
        self_confidence=0.95,
    )

    pool, _ = await _run_process_image(field=field, extraction=extraction)

    row = _cell_kwargs(_cell_insert_calls(pool)[0])
    assert row["validation_error"] is not None
    assert "unit mismatch" in row["validation_error"]
    assert "V" in row["validation_error"]
    assert "mA" in row["validation_error"]
    assert row["status"] == "needs_review"


@pytest.mark.asyncio
async def test_crop_upload_failure_writes_cell_with_null_crop_path() -> None:
    """Crop upload exception → cell still written with crop_path=None (R3)."""
    field = _make_field("Voltage")
    extraction = ExtractionResponse(
        raw_text="12.34",
        parsed_value=12.34,
        unit_seen=None,
        legible=True,
        self_confidence=0.95,
    )

    pool, _ = await _run_process_image(
        field=field,
        extraction=extraction,
        upload_raises=RuntimeError("S3 unreachable"),
    )

    inserts = _cell_insert_calls(pool)
    assert len(inserts) == 1
    row = _cell_kwargs(inserts[0])
    # Cell written, crop_path is None.
    assert row["crop_path"] is None
    # Extraction still succeeded → ok status (high confidence, legible).
    assert row["status"] == "ok"


@pytest.mark.asyncio
async def test_extraction_failure_writes_failed_cell() -> None:
    """Both Gemini retries raise → status='failed', failure_reason='extraction_failed'."""
    field = _make_field("Voltage")

    pool, _ = await _run_process_image(
        field=field,
        extraction_raises=RuntimeError("gemini exploded"),
    )

    inserts = _cell_insert_calls(pool)
    assert len(inserts) == 1
    row = _cell_kwargs(inserts[0])
    assert row["status"] == "failed"
    assert row["failure_reason"] == "extraction_failed"
    assert row["parsed_value"] is None
    assert row["raw_text"] is None

    # image_tasks should be marked 'failed' (no successful field on this image).
    task_updates = [
        c.args
        for c in pool.execute.await_args_list
        if "UPDATE image_tasks" in c.args[0] and "status" in c.args[0]
    ]
    assert task_updates[-1][2] == "failed"


@pytest.mark.asyncio
async def test_combined_confidence_written_correctly() -> None:
    """combined_confidence = round(0.4*box + 0.6*value, 3) — verify written value."""
    field = _make_field("Voltage")
    extraction = ExtractionResponse(
        raw_text="42",
        parsed_value=42,
        unit_seen=None,
        legible=True,
        self_confidence=0.7,
    )

    pool, _ = await _run_process_image(
        field=field,
        extraction=extraction,
        detection_overrides={"box_confidence": 0.8},
    )

    row = _cell_kwargs(_cell_insert_calls(pool)[0])
    # 0.4 * 0.8 + 0.6 * 0.7 = 0.32 + 0.42 = 0.74
    assert row["combined_confidence"] == pytest.approx(0.74, abs=1e-6)
    assert row["box_confidence"] == pytest.approx(0.8)
    assert row["value_confidence"] == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# PaddleOCR reconciliation (Slice 3.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ocr_agrees_with_flash_uses_max_confidence() -> None:
    """Both readers agree → value_confidence = max(flash_conf, ocr_conf=0.85)."""
    field = _make_field("Voltage", expected_format={"type": "number"})
    extraction = ExtractionResponse(
        raw_text="12.34",
        parsed_value=12.34,
        unit_seen=None,
        legible=True,
        self_confidence=0.7,  # < ocr_conf so we can see max() pick OCR's 0.85.
    )

    pool, _ = await _run_process_image(
        field=field,
        extraction=extraction,
        ocr_text="12.34",
    )

    row = _cell_kwargs(_cell_insert_calls(pool)[0])
    assert row["value_confidence"] == pytest.approx(0.85)
    # Flash value preserved when readings agree.
    assert json.loads(row["parsed_value"]) == 12.34
    assert row["legible"] is True


@pytest.mark.asyncio
async def test_ocr_disagrees_sets_legible_false() -> None:
    """Readers disagree → legible=False, value_confidence = min(flash, ocr) − 0.2."""
    field = _make_field("Voltage", expected_format={"type": "number"})
    extraction = ExtractionResponse(
        raw_text="12.34",
        parsed_value=12.34,
        unit_seen=None,
        legible=True,
        self_confidence=0.9,  # min(0.9, 0.85) = 0.85, − 0.2 = 0.65.
    )

    pool, _ = await _run_process_image(
        field=field,
        extraction=extraction,
        ocr_text="56.78",
    )

    row = _cell_kwargs(_cell_insert_calls(pool)[0])
    assert row["legible"] is False
    assert row["value_confidence"] == pytest.approx(0.65, abs=1e-6)
    # legible=False forces needs_review regardless of combined_confidence.
    assert row["status"] == "needs_review"


@pytest.mark.asyncio
async def test_flash_not_legible_but_ocr_succeeds_uses_ocr() -> None:
    """Flash legible=False + OCR text → legible flipped to True, OCR value used."""
    field = _make_field("Voltage", expected_format={"type": "number"})
    extraction = ExtractionResponse(
        raw_text="??",
        parsed_value=None,
        unit_seen=None,
        legible=False,
        self_confidence=0.2,
    )

    pool, _ = await _run_process_image(
        field=field,
        extraction=extraction,
        ocr_text="42",
    )

    row = _cell_kwargs(_cell_insert_calls(pool)[0])
    assert row["legible"] is True
    assert row["raw_text"] == "42"
    # OCR's "42" was parsed as a number per expected_format.
    assert json.loads(row["parsed_value"]) == 42.0
    assert row["value_confidence"] == pytest.approx(0.85)
