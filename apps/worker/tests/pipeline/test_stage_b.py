"""Unit tests for Stage B (region detection) box conversion and orchestration.

All Gemini calls are mocked. No real network access. No database — the
asyncpg pool is replaced with an ``AsyncMock`` so we can assert on the
SQL calls the orchestrator makes without standing up Postgres.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.pipeline import stage_b
from app.pipeline.schemas import DetectedField, DetectionResponse
from app.pipeline.stage_b import convert_box_to_normalized

# ---------------------------------------------------------------------------
# Box coordinate conversion (pure function)
# ---------------------------------------------------------------------------


def test_box_conversion_valid() -> None:
    """[ymin=100, xmin=200, ymax=300, xmax=400] → x=0.2 y=0.1 w=0.2 h=0.2."""
    box = convert_box_to_normalized([100, 200, 300, 400])
    assert box == {"x": 0.2, "y": 0.1, "width": 0.2, "height": 0.2}


def test_box_conversion_clamps() -> None:
    """Out-of-range values clamp to [0, 1]."""
    # Negative ymin/xmin and > 1000 ymax/xmax. Clamp keeps everything in [0, 1].
    box = convert_box_to_normalized([-50, -100, 1500, 1200])
    assert 0.0 <= box["x"] <= 1.0
    assert 0.0 <= box["y"] <= 1.0
    assert 0.0 <= box["width"] <= 1.0
    assert 0.0 <= box["height"] <= 1.0
    # Ensure the underflow values clamped specifically to 0 rather than rounding.
    assert box["x"] == 0.0
    assert box["y"] == 0.0


def test_box_conversion_inverted_clamps_to_zero_size() -> None:
    """Inverted boxes (xmax < xmin) collapse to zero width/height, not negative."""
    box = convert_box_to_normalized([400, 600, 200, 300])  # ymax<ymin, xmax<xmin
    assert box["width"] == 0.0
    assert box["height"] == 0.0


# ---------------------------------------------------------------------------
# Per-image orchestration — drives _process_image with mocks
# ---------------------------------------------------------------------------


def _stub_pool() -> AsyncMock:
    """Build an async-mock asyncpg pool with the methods Stage B uses."""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value=None)
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    return pool


def _make_job() -> dict:
    return {"id": uuid4(), "project_id": uuid4()}


def _make_task(*, width_px: int = 1000, height_px: int = 800) -> dict:
    return {
        "task_id": uuid4(),
        "image_id": uuid4(),
        "status": "pending",
        "storage_path": "projects/x/originals/y.jpg",
        "width_px": width_px,
        "height_px": height_px,
    }


def _make_field(field_name: str) -> dict:
    return {
        "id": uuid4(),
        "field_name": field_name,
        "semantic_description": f"semantic for {field_name}",
    }


def _failed_cell_field_ids(pool: AsyncMock) -> list[Any]:
    """Extract the field_id positional arg passed to every _write_failed_cell INSERT."""
    field_ids = []
    for call in pool.execute.await_args_list:
        sql = call.args[0]
        if "INSERT INTO cells" in sql and "failed" in sql:
            # signature: (sql, job_id, project_id, image_id, field_id, box_conf, failure, model_versions_json)
            field_ids.append(call.args[4])
    return field_ids


async def _run_process_image(
    *,
    fields: list[dict],
    detection_response: DetectionResponse,
    task: dict | None = None,
) -> tuple[AsyncMock, dict]:
    pool = _stub_pool()
    job = _make_job()
    task = task or _make_task()
    detected: dict = {}

    with (
        patch.object(stage_b, "_download_image_bytes", return_value=b"raw"),
        patch.object(
            stage_b, "_open_dimensions", return_value=(task["width_px"], task["height_px"])
        ),
        patch.object(stage_b, "preprocess_for_stage_b", return_value=b"preprocessed"),
        patch.object(stage_b, "_call_with_retry", new=AsyncMock(return_value=detection_response)),
    ):
        await stage_b._process_image(
            pool,
            job=job,
            fields=fields,
            task=task,
            client=object(),
            detected=detected,
            progress_lock=__import__("asyncio").Lock(),
        )

    return pool, detected


@pytest.mark.asyncio
async def test_missing_field_in_response_writes_failed_cell() -> None:
    """Gemini returns 2 of 3 fields; missing field gets a failed cell with field_not_visible."""
    f_voltage = _make_field("Voltage")
    f_current = _make_field("Current")
    f_temp = _make_field("Temperature")
    fields = [f_voltage, f_current, f_temp]

    response = DetectionResponse(
        fields=[
            DetectedField(
                field_name="Voltage",
                found=True,
                box=[100, 200, 300, 400],
                box_confidence=0.9,
            ),
            DetectedField(
                field_name="Current",
                found=True,
                box=[100, 200, 300, 400],
                box_confidence=0.85,
            ),
            # Temperature is intentionally absent.
        ]
    )

    pool, detected = await _run_process_image(fields=fields, detection_response=response)

    failed_ids = _failed_cell_field_ids(pool)
    assert f_temp["id"] in failed_ids
    # The two present fields should be in the detected map, not failed.
    assert f_voltage["id"] in next(iter(detected.values()))
    assert f_current["id"] in next(iter(detected.values()))
    assert f_voltage["id"] not in failed_ids
    assert f_current["id"] not in failed_ids

    # Image task ends in 'extracting' since at least one detection succeeded.
    statuses = [
        c.args
        for c in pool.execute.await_args_list
        if "UPDATE image_tasks" in c.args[0] and "status" in c.args[0]
    ]
    last_task_update = statuses[-1]
    assert last_task_update[2] == "extracting"


@pytest.mark.asyncio
async def test_found_false_writes_failed_cell() -> None:
    """found=False on a field → failed cell, no entry in detected map."""
    f = _make_field("Voltage")

    response = DetectionResponse(
        fields=[
            DetectedField(field_name="Voltage", found=False, box=None, box_confidence=0.0),
        ]
    )

    pool, detected = await _run_process_image(fields=[f], detection_response=response)

    assert detected == {}
    failed_ids = _failed_cell_field_ids(pool)
    assert failed_ids == [f["id"]]

    # Status must be 'not_found' since zero fields detected.
    last_status_update = [
        c.args
        for c in pool.execute.await_args_list
        if "UPDATE image_tasks" in c.args[0] and "status" in c.args[0]
    ][-1]
    assert last_status_update[2] == "not_found"


@pytest.mark.asyncio
async def test_invalid_box_treated_as_not_found() -> None:
    """Box that decodes fine but fails is_box_valid → failed cell, not in detected map."""
    f = _make_field("Voltage")
    # 1x1 box on a 1000x800 image — area 1 px², below MIN_AREA_PX threshold.
    # In 0..1000 space: ymin=500, xmin=500, ymax=501, xmax=501.
    response = DetectionResponse(
        fields=[
            DetectedField(
                field_name="Voltage",
                found=True,
                box=[500, 500, 501, 501],
                box_confidence=0.9,
            ),
        ]
    )

    pool, detected = await _run_process_image(fields=[f], detection_response=response)

    assert detected == {}
    failed_ids = _failed_cell_field_ids(pool)
    assert failed_ids == [f["id"]]


@pytest.mark.asyncio
async def test_valid_detection_returns_box_dict() -> None:
    """Valid box returned correctly normalized in the detected map."""
    f = _make_field("Voltage")
    # Box covering middle 20% of image: ymin=100..300 (10..30%), xmin=200..400 (20..40%).
    response = DetectionResponse(
        fields=[
            DetectedField(
                field_name="Voltage",
                found=True,
                box=[100, 200, 300, 400],
                box_confidence=0.9,
            ),
        ]
    )

    task = _make_task(width_px=1000, height_px=800)
    pool, detected = await _run_process_image(fields=[f], detection_response=response, task=task)

    assert _failed_cell_field_ids(pool) == []
    assert task["image_id"] in detected
    field_payload = detected[task["image_id"]][f["id"]]
    assert field_payload["box"] == {"x": 0.2, "y": 0.1, "width": 0.2, "height": 0.2}
    assert field_payload["box_confidence"] == pytest.approx(0.9)
    assert field_payload["image_width"] == 1000
    assert field_payload["image_height"] == 800
