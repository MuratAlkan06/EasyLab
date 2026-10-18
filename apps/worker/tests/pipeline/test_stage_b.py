"""Unit tests for Stage B (region detection) orchestration.

The aligner is mocked (its own tests live in test_align.py). No real network
access. No database — the asyncpg pool is replaced with an ``AsyncMock`` so we
can assert on the SQL calls the orchestrator makes without standing up Postgres.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from app.pipeline import stage_b
from app.pipeline.align import AlignedBox, AlignResult

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
    return {"id": uuid4(), "field_name": field_name}


def _failed_cell_field_ids(pool: AsyncMock) -> list[Any]:
    """Extract the field_id positional arg passed to every _write_failed_cell INSERT."""
    field_ids = []
    for call in pool.execute.await_args_list:
        sql = call.args[0]
        if "INSERT INTO cells" in sql and "failed" in sql:
            # signature: (sql, job_id, project_id, image_id, field_id, box_conf, failure, model_versions_json)
            field_ids.append(call.args[4])
    return field_ids


_BOX = {"x": 0.2, "y": 0.1, "width": 0.2, "height": 0.2}


def _aligned(boxes: dict[Any, AlignedBox]) -> AlignResult:
    return AlignResult(fields=boxes, h_status="strong", matches=100, inliers=90, homography=None)


async def _run_process_image(
    *,
    fields: list[dict],
    align_result: AlignResult | None = None,
    align_error: Exception | None = None,
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
        patch.object(
            stage_b, "align_target", MagicMock(return_value=align_result, side_effect=align_error)
        ),
    ):
        await stage_b._process_image(
            pool,
            job=job,
            fields=fields,
            task=task,
            reference=object(),
            detected=detected,
            progress_lock=__import__("asyncio").Lock(),
        )

    return pool, detected


@pytest.mark.asyncio
async def test_missing_field_in_response_writes_failed_cell() -> None:
    """Aligner returns 2 of 3 fields; missing field gets a failed cell with field_not_visible."""
    f_voltage = _make_field("Voltage")
    f_current = _make_field("Current")
    f_temp = _make_field("Temperature")
    fields = [f_voltage, f_current, f_temp]

    response = _aligned(
        {
            f_voltage["id"]: AlignedBox(_BOX, 0.93, "homography+ncc"),
            f_current["id"]: AlignedBox(_BOX, 0.85, "homography"),
            # Temperature is intentionally absent (it had no reference box).
        }
    )

    pool, detected = await _run_process_image(fields=fields, align_result=response)

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
    """Aligner fell back (field not located) and no usable reference box → failed
    cell, no entry in detected map."""
    f = _make_field("Voltage")

    response = _aligned({f["id"]: AlignedBox(_BOX, 0.55, "fallback")})

    pool, detected = await _run_process_image(fields=[f], align_result=response)

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
    tiny = {"x": 0.5, "y": 0.5, "width": 0.001, "height": 0.001}
    response = _aligned({f["id"]: AlignedBox(tiny, 0.85, "homography")})

    pool, detected = await _run_process_image(fields=[f], align_result=response)

    assert detected == {}
    failed_ids = _failed_cell_field_ids(pool)
    assert failed_ids == [f["id"]]


@pytest.mark.asyncio
async def test_valid_detection_returns_box_dict() -> None:
    """Valid box returned correctly normalized in the detected map."""
    f = _make_field("Voltage")
    response = _aligned({f["id"]: AlignedBox(_BOX, 0.85, "homography")})

    task = _make_task(width_px=1000, height_px=800)
    pool, detected = await _run_process_image(fields=[f], align_result=response, task=task)

    assert _failed_cell_field_ids(pool) == []
    assert task["image_id"] in detected
    field_payload = detected[task["image_id"]][f["id"]]
    assert field_payload["box"] == {"x": 0.2, "y": 0.1, "width": 0.2, "height": 0.2}
    assert field_payload["box_confidence"] == pytest.approx(0.85)
    assert field_payload["image_width"] == 1000
    assert field_payload["image_height"] == 800


# ---------------------------------------------------------------------------
# ADR-007 Slice 3: aligner wiring (fallback rungs, telemetry, reference once)
# ---------------------------------------------------------------------------

_REF_BOX = {"x": 0.3, "y": 0.3, "width": 0.2, "height": 0.1}


@pytest.mark.asyncio
async def test_aligner_fallback_uses_reference_box_at_fallback_confidence() -> None:
    f = {**_make_field("Voltage"), "reference_box": _REF_BOX}
    response = _aligned({f["id"]: AlignedBox(_REF_BOX, 0.55, "fallback")})

    task = _make_task()
    pool, detected = await _run_process_image(fields=[f], align_result=response, task=task)

    assert _failed_cell_field_ids(pool) == []
    payload = detected[task["image_id"]][f["id"]]
    assert payload["box"] == _REF_BOX
    assert payload["box_confidence"] == stage_b._REFERENCE_BOX_CONFIDENCE


@pytest.mark.asyncio
async def test_failed_cells_record_the_aligner_version() -> None:
    """model_versions.detector names the aligner + the OpenCV build (A6)."""
    import cv2

    f = _make_field("Voltage")
    pool, _ = await _run_process_image(fields=[f], align_result=_aligned({}))

    (insert,) = [c for c in pool.execute.await_args_list if "INSERT INTO cells" in c.args[0]]
    assert json.loads(insert.args[7]) == {"detector": f"align-v1+opencv{cv2.__version__}"}


@pytest.mark.asyncio
async def test_align_error_falls_back_per_field_like_the_old_detector_error_path() -> None:
    """align_target raising → reference fallback where a box exists, failed cell otherwise."""
    with_ref = {**_make_field("Voltage"), "reference_box": _REF_BOX}
    without_ref = _make_field("Current")

    task = _make_task()
    pool, detected = await _run_process_image(
        fields=[with_ref, without_ref], align_error=RuntimeError("cv2 exploded"), task=task
    )

    assert detected[task["image_id"]][with_ref["id"]]["box_confidence"] == 0.55
    assert _failed_cell_field_ids(pool) == [without_ref["id"]]


@pytest.mark.asyncio
async def test_detector_raw_telemetry_and_align_latency_are_logged() -> None:
    """The per-field raw-box + IoU event survives (relabelled detector-neutral) and
    every image logs its align latency (A10)."""
    f = {**_make_field("Voltage"), "reference_box": _REF_BOX}
    g = {**_make_field("Current"), "reference_box": _REF_BOX}
    response = _aligned(
        {
            f["id"]: AlignedBox(_REF_BOX, 0.93, "homography+ncc", ncc_peak=0.8, ncc_shift=0.0),
            g["id"]: AlignedBox(_REF_BOX, 0.55, "fallback"),
        }
    )

    with capture_logs() as logs:
        await _run_process_image(fields=[f, g], align_result=response)

    raw = {e["field_name"]: e for e in logs if e["event"] == "stage_b.detector_raw"}
    assert raw["Voltage"]["box_iou_vs_reference"] == 1.0
    assert raw["Voltage"]["method"] == "homography+ncc"
    assert raw["Current"]["method"] == "fallback"
    assert raw["Current"]["found"] is False
    assert raw["Current"]["box_iou_vs_reference"] is None
    assert not any(e["event"] == "stage_b.gemini_raw" for e in logs)
    (aligned_event,) = [e for e in logs if e["event"] == "stage_b.image.aligned"]
    assert aligned_event["align_ms"] >= 0
    assert aligned_event["h_status"] == "strong"


@pytest.mark.asyncio
async def test_load_template_fields_returns_only_what_the_aligner_uses() -> None:
    """id + field_name + parsed reference_box. semantic_description is a Stage A
    output the aligner never reads, so Stage B no longer selects it."""
    field_id = uuid4()
    pool = _stub_pool()
    pool.fetch = AsyncMock(
        return_value=[
            {"id": field_id, "field_name": "Voltage", "reference_box": json.dumps(_REF_BOX)},
            {"id": uuid4(), "field_name": "Current", "reference_box": None},
        ]
    )

    fields = await stage_b._load_template_fields(pool, uuid4())

    assert fields[0] == {"id": field_id, "field_name": "Voltage", "reference_box": _REF_BOX}
    assert fields[1]["reference_box"] is None
    assert "semantic_description" not in pool.fetch.await_args.args[0]


@pytest.mark.asyncio
async def test_run_stage_b_prepares_reference_once_per_job() -> None:
    """One reference download + feature pass per job, shared by every image."""
    pool = _stub_pool()
    job = _make_job()
    fields = [{**_make_field("Voltage"), "reference_box": _REF_BOX}]
    tasks = [_make_task() for _ in range(3)]
    reference = MagicMock(points=[])
    downloads: list[str] = []

    def download(path: str) -> bytes:
        downloads.append(path)
        return b"bytes"

    prepare = MagicMock(return_value=reference)
    with (
        patch.object(stage_b, "_load_template_fields", AsyncMock(return_value=fields)),
        patch.object(stage_b, "_load_image_tasks", AsyncMock(return_value=tasks)),
        patch.object(stage_b, "_load_reference_storage_path", AsyncMock(return_value="ref.jpg")),
        patch.object(stage_b, "_download_image_bytes", side_effect=download),
        patch.object(stage_b, "_open_dimensions", return_value=(1000, 800)),
        patch.object(stage_b, "prepare_reference", prepare),
        patch.object(stage_b, "align_target", return_value=_aligned({})) as align_target,
    ):
        await stage_b.run_stage_b(pool, job)

    prepare.assert_called_once_with(b"bytes", {fields[0]["id"]: _REF_BOX})
    assert downloads.count("ref.jpg") == 1
    assert len(downloads) == 1 + len(tasks)
    assert align_target.call_count == len(tasks)
    assert all(call.args[0] is reference for call in align_target.call_args_list)
