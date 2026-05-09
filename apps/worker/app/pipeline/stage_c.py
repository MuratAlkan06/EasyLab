"""Stage C — Value Extraction.

Inputs:
  * Per-image, per-field detected boxes from Stage B (normalized 0..1, plus
    box_confidence and original-resolution image dimensions).
  * `template_fields` rows for the project (field names and expected_format).

Outputs:
  * One ``cells`` row per (image, field) detected by Stage B with the typed
    parsed value, raw text, confidences, status, and crop_path.
  * Crop JPEGs uploaded to Supabase Storage at
    ``projects/{project_id}/crops/{image_id}/{field_id}.jpg``.
  * `image_tasks.status` advanced to ``done`` when all fields succeed,
    ``failed`` if every field's extraction failed.

PaddleOCR runs in parallel with Gemini Flash on the same crop and the two
readings are reconciled per docs/ai-pipeline.md §7. When ``OCR_ENABLED=false``
the reconciliation falls through to the Flash-only path
(``value_confidence = extraction.self_confidence``).
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from typing import Any
from uuid import UUID

import asyncpg
import structlog
from PIL import Image
from supabase import create_client
from tenacity import (
    AsyncRetrying,
    RetryError,
    stop_after_attempt,
    wait_fixed,
)

from app.pipeline.confidence import compute_cell_status, compute_combined_confidence
from app.pipeline.crop_utils import compute_crop_rect, crop_and_encode
from app.pipeline.gemini import call_stage_c, get_client
from app.pipeline.paddle_ocr import (
    OCR_FAIL_CONFIDENCE,
    OCR_SEMAPHORE,
    OCR_SUCCESS_CONFIDENCE,
    parse_ocr_value,
    run_ocr,
)
from app.pipeline.schemas import ExtractionResponse
from app.settings import settings

log = structlog.get_logger()

# Two attempts total for the Gemini Flash extraction call (mirrors Stage B).
_RETRY_WAIT_SECONDS = 2.0
_MAX_ATTEMPTS = 2

# Heartbeat the parent job at most this often during Stage C.
_HEARTBEAT_INTERVAL_SECONDS = 5.0


async def _call_with_retry(
    client, model_name: str, crop_bytes: bytes, field: dict
) -> ExtractionResponse:
    """Call Stage C and validate; retry once on any failure (tenacity)."""
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_fixed(_RETRY_WAIT_SECONDS),
            reraise=True,
        ):
            with attempt:
                return await call_stage_c(client, model_name, crop_bytes, field)
    except RetryError as exc:  # defensive: reraise=True should already unwrap
        raise exc.last_attempt.exception() from exc
    raise RuntimeError("unreachable: AsyncRetrying exited without yielding")


async def _load_template_fields(pool: asyncpg.Pool, project_id) -> dict[UUID, dict]:
    """Return ``{field_id: {"id", "field_name", "expected_format"}}`` for the project."""
    rows = await pool.fetch(
        """
        SELECT id, field_name, expected_format
        FROM template_fields
        WHERE project_id = $1
        ORDER BY display_order
        """,
        project_id,
    )
    out: dict[UUID, dict] = {}
    for r in rows:
        ef = r["expected_format"]
        # asyncpg returns jsonb as str by default unless a codec is registered.
        if isinstance(ef, str):
            ef = json.loads(ef)
        out[r["id"]] = {
            "id": r["id"],
            "field_name": r["field_name"],
            "expected_format": ef,
        }
    return out


async def _load_image_tasks_by_image(
    pool: asyncpg.Pool, job_id, image_ids: list[UUID]
) -> dict[UUID, dict]:
    """Return ``{image_id: {task_id, storage_path}}`` for the given image_ids."""
    if not image_ids:
        return {}
    rows = await pool.fetch(
        """
        SELECT t.id           AS task_id,
               t.image_id     AS image_id,
               i.storage_path AS storage_path
        FROM image_tasks t
        JOIN images i ON i.id = t.image_id
        WHERE t.job_id = $1
          AND t.image_id = ANY($2::uuid[])
        """,
        job_id,
        list(image_ids),
    )
    return {r["image_id"]: dict(r) for r in rows}


def _download_image_bytes(storage_path: str) -> bytes:
    """Download an image from Supabase Storage (sync supabase-py)."""
    supabase = create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    return supabase.storage.from_(settings.supabase_storage_bucket).download(storage_path)


def _upload_crop_bytes(storage_path: str, crop_bytes: bytes) -> None:
    """Upload a crop JPEG to Supabase Storage with upsert (sync supabase-py)."""
    supabase = create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    supabase.storage.from_(settings.supabase_storage_bucket).upload(
        storage_path,
        crop_bytes,
        {"content-type": "image/jpeg", "upsert": "true"},
    )


def _open_image(img_bytes: bytes) -> Image.Image:
    """Open and load image from raw bytes for cropping."""
    img = Image.open(io.BytesIO(img_bytes))
    img.load()
    return img


def _validate_extraction(
    extraction: ExtractionResponse, expected_format: dict | None
) -> str | None:
    """Return a validation_error string, or None if the extraction matches the expected_format."""
    if not expected_format:
        return None
    if extraction.parsed_value is None:
        # Failed extraction is handled separately; no validation_error here.
        return None

    expected_type = expected_format.get("type")
    if expected_type == "number":
        if not isinstance(extraction.parsed_value, (int, float)) or isinstance(
            extraction.parsed_value, bool
        ):
            return "expected number, got string"

    expected_unit = expected_format.get("unit")
    if expected_unit:
        seen = (extraction.unit_seen or "").strip().lower()
        want = expected_unit.strip().lower()
        if seen != want:
            return f"unit mismatch: expected {expected_unit}, got {extraction.unit_seen or ''}"
    return None


async def _write_cell(
    pool: asyncpg.Pool,
    *,
    job_id: UUID,
    project_id: UUID,
    image_id: UUID,
    field_id: UUID,
    raw_text: str | None,
    parsed_value: Any,
    unit_seen: str | None,
    legible: bool | None,
    box_confidence: float,
    value_confidence: float,
    combined_confidence: float | None,
    status: str,
    failure_reason: str | None,
    validation_error: str | None,
    detected_box: dict | None,
    crop_path: str | None,
) -> None:
    """Upsert a cells row (one row per (job, image, field))."""
    model_versions = json.dumps(
        {
            "detector": settings.gemini_detection_model,
            "extractor": settings.gemini_extraction_model,
        }
    )
    parsed_value_json = json.dumps(parsed_value) if parsed_value is not None else None
    detected_box_json = json.dumps(detected_box) if detected_box is not None else None

    await pool.execute(
        """
        INSERT INTO cells (
            job_id, project_id, image_id, field_id,
            raw_text, parsed_value, unit_seen, legible,
            box_confidence, value_confidence, combined_confidence,
            status, failure_reason, validation_error,
            detected_box, crop_path, model_versions
        ) VALUES (
            $1, $2, $3, $4,
            $5, $6::jsonb, $7, $8,
            $9, $10, $11,
            $12, $13, $14,
            $15::jsonb, $16, $17::jsonb
        )
        ON CONFLICT (job_id, image_id, field_id) DO UPDATE
        SET raw_text            = excluded.raw_text,
            parsed_value        = excluded.parsed_value,
            unit_seen           = excluded.unit_seen,
            legible             = excluded.legible,
            box_confidence      = excluded.box_confidence,
            value_confidence    = excluded.value_confidence,
            combined_confidence = excluded.combined_confidence,
            status              = excluded.status,
            failure_reason      = excluded.failure_reason,
            validation_error    = excluded.validation_error,
            detected_box        = excluded.detected_box,
            crop_path           = excluded.crop_path,
            model_versions      = excluded.model_versions,
            updated_at          = now()
        """,
        job_id,
        project_id,
        image_id,
        field_id,
        raw_text,
        parsed_value_json,
        unit_seen,
        legible,
        box_confidence,
        value_confidence,
        combined_confidence,
        status,
        failure_reason,
        validation_error,
        detected_box_json,
        crop_path,
        model_versions,
    )


async def _process_field(
    pool: asyncpg.Pool,
    *,
    job_id: UUID,
    project_id: UUID,
    image_id: UUID,
    field: dict,
    detection: dict,
    full_image: Image.Image,
    client,
) -> bool:
    """Run extraction for one (image, field). Returns True if status != 'failed'."""
    field_id: UUID = field["id"]
    box = detection["box"]
    box_confidence = float(detection["box_confidence"])
    width_px = int(detection["image_width"])
    height_px = int(detection["image_height"])

    # --- Crop generation -----------------------------------------------------
    crop_rect = compute_crop_rect(box, width_px, height_px)
    crop_bytes = await asyncio.to_thread(crop_and_encode, full_image, crop_rect)

    # --- Crop upload (failure here must NOT abort the cell write — R3) -------
    crop_storage_path = f"projects/{project_id}/crops/{image_id}/{field_id}.jpg"
    crop_path: str | None = crop_storage_path
    try:
        await asyncio.to_thread(_upload_crop_bytes, crop_storage_path, crop_bytes)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "stage_c.crop_upload_failed",
            job_id=str(job_id),
            image_id=str(image_id),
            field_id=str(field_id),
            error=str(exc),
        )
        crop_path = None

    # --- Gemini Flash extraction --------------------------------------------
    try:
        extraction = await _call_with_retry(
            client,
            settings.gemini_extraction_model,
            crop_bytes,
            {
                "field_name": field["field_name"],
                "expected_format": field.get("expected_format"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "stage_c.extraction_failed",
            job_id=str(job_id),
            image_id=str(image_id),
            field_id=str(field_id),
            error=str(exc),
        )
        await _write_cell(
            pool,
            job_id=job_id,
            project_id=project_id,
            image_id=image_id,
            field_id=field_id,
            raw_text=None,
            parsed_value=None,
            unit_seen=None,
            legible=None,
            box_confidence=box_confidence,
            value_confidence=0.0,
            combined_confidence=None,
            status="failed",
            failure_reason="extraction_failed",
            validation_error=None,
            detected_box=box,
            crop_path=crop_path,
        )
        return False

    # --- PaddleOCR reconciliation (Slice 3.4) -------------------------------
    # Run OCR (gated by OCR_ENABLED). Blocking → asyncio.to_thread, capped by
    # OCR_SEMAPHORE so a burst of crops can't pin every CPU core.
    if settings.ocr_enabled:
        async with OCR_SEMAPHORE:
            ocr_text = await asyncio.to_thread(run_ocr, crop_bytes)
    else:
        ocr_text = None

    flash_conf = float(extraction.self_confidence)
    ocr_conf = OCR_SUCCESS_CONFIDENCE if ocr_text else OCR_FAIL_CONFIDENCE

    # Working copies of the extracted signals — reconciliation may overwrite.
    raw_text: str | None = extraction.raw_text
    parsed_value: Any = extraction.parsed_value
    unit_seen: str | None = extraction.unit_seen
    legible: bool = extraction.legible

    expected_format = field.get("expected_format")
    ocr_parsed = parse_ocr_value(ocr_text, expected_format) if ocr_text else None

    if ocr_text is None:
        # Row 3: OCR failed/empty → use Flash as-is.
        value_confidence = flash_conf
    elif not extraction.legible:
        # Row 4: Flash illegible but OCR produced a value → use OCR.
        value_confidence = ocr_conf
        raw_text = ocr_text
        parsed_value = ocr_parsed if ocr_parsed is not None else ocr_text
        legible = True
    else:
        flash_norm = str(extraction.parsed_value).strip().lower()
        ocr_norm = str(ocr_parsed if ocr_parsed is not None else ocr_text).strip().lower()
        if flash_norm == ocr_norm:
            # Row 1: agree → keep Flash value, take the more confident reading.
            value_confidence = max(flash_conf, ocr_conf)
        else:
            # Row 2: disagree → mark illegible so status falls to needs_review.
            value_confidence = min(flash_conf, ocr_conf) - 0.2
            legible = False

    # --- Validation against expected_format ---------------------------------
    validation_error = _validate_extraction(extraction, expected_format)

    # --- Confidence + status ------------------------------------------------
    combined = compute_combined_confidence(box_confidence, value_confidence)
    status = compute_cell_status(
        combined_confidence=combined,
        legible=legible,
        validation_error=validation_error,
        found=True,
        parsed_value_is_null=parsed_value is None,
    )

    failure_reason = "extraction_failed" if status == "failed" else None

    await _write_cell(
        pool,
        job_id=job_id,
        project_id=project_id,
        image_id=image_id,
        field_id=field_id,
        raw_text=raw_text,
        parsed_value=parsed_value,
        unit_seen=unit_seen,
        legible=legible,
        box_confidence=box_confidence,
        value_confidence=value_confidence,
        combined_confidence=combined,
        status=status,
        failure_reason=failure_reason,
        validation_error=validation_error,
        detected_box=box,
        crop_path=crop_path,
    )
    return status != "failed"


async def _process_image(
    pool: asyncpg.Pool,
    *,
    job: dict,
    image_id: UUID,
    detections: dict[UUID, dict],
    fields_by_id: dict[UUID, dict],
    image_task: dict,
    client,
    progress_lock: asyncio.Lock,
) -> None:
    """Run Stage C for every detected field on one image."""
    job_id: UUID = job["id"]
    project_id: UUID = job["project_id"]
    storage_path: str = image_task["storage_path"]
    task_id: UUID = image_task["task_id"]

    log.info(
        "stage_c.image.start",
        job_id=str(job_id),
        image_id=str(image_id),
        field_count=len(detections),
    )

    # Download original full-resolution image once and crop from it.
    try:
        raw_bytes = await asyncio.to_thread(_download_image_bytes, storage_path)
        full_image = await asyncio.to_thread(_open_image, raw_bytes)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "stage_c.image.download_failed",
            job_id=str(job_id),
            image_id=str(image_id),
            error=str(exc),
        )
        # Mark every detected field as a failed cell — extraction never ran.
        for field_id, detection in detections.items():
            field = fields_by_id.get(field_id)
            if field is None:
                continue
            await _write_cell(
                pool,
                job_id=job_id,
                project_id=project_id,
                image_id=image_id,
                field_id=field_id,
                raw_text=None,
                parsed_value=None,
                unit_seen=None,
                legible=None,
                box_confidence=float(detection["box_confidence"]),
                value_confidence=0.0,
                combined_confidence=None,
                status="failed",
                failure_reason="extraction_failed",
                validation_error=None,
                detected_box=detection["box"],
                crop_path=None,
            )
        await pool.execute(
            """
            UPDATE image_tasks
            SET status = 'failed', error = $2, updated_at = now()
            WHERE id = $1
            """,
            task_id,
            str(exc)[:500],
        )
        return

    any_success = False
    try:
        for field_id, detection in detections.items():
            field = fields_by_id.get(field_id)
            if field is None:
                # Defensive: a detection refers to a field no longer in the template.
                continue
            ok = await _process_field(
                pool,
                job_id=job_id,
                project_id=project_id,
                image_id=image_id,
                field=field,
                detection=detection,
                full_image=full_image,
                client=client,
            )
            any_success = any_success or ok
    finally:
        try:
            full_image.close()
        except Exception:  # noqa: BLE001
            pass

    new_task_status = "done" if any_success else "failed"
    await pool.execute(
        "UPDATE image_tasks SET status = $2, updated_at = now() WHERE id = $1",
        task_id,
        new_task_status,
    )

    if any_success:
        await pool.execute(
            "UPDATE images SET status = 'done', updated_at = now() WHERE id = $1",
            image_id,
        )

    # Per-image progress + heartbeat.
    async with progress_lock:
        await pool.execute(
            """
            UPDATE jobs
            SET progress_done = progress_done + 1,
                heartbeat_at  = now()
            WHERE id = $1
            """,
            job_id,
        )

    log.info(
        "stage_c.image.complete",
        job_id=str(job_id),
        image_id=str(image_id),
        status=new_task_status,
    )


async def _heartbeat_loop(pool: asyncpg.Pool, job_id: UUID, stop: asyncio.Event) -> None:
    """Bump ``jobs.heartbeat_at`` every few seconds until ``stop`` is set."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            pass
        if stop.is_set():
            return
        await pool.execute(
            "UPDATE jobs SET heartbeat_at = now() WHERE id = $1",
            job_id,
        )


async def run_stage_c(
    pool: asyncpg.Pool,
    job: dict,
    detected_boxes: dict[UUID, dict[UUID, dict[str, Any]]],
) -> None:
    """Run Stage C for every image with at least one detected field."""
    project_id = job["project_id"]
    job_id: UUID = job["id"]
    log.info(
        "stage_c.start",
        job_id=str(job_id),
        project_id=str(project_id),
        image_count=len(detected_boxes),
    )

    if not detected_boxes:
        log.info("stage_c.no_detections", job_id=str(job_id))
        return

    fields_by_id = await _load_template_fields(pool, project_id)
    if not fields_by_id:
        raise RuntimeError(f"Stage C: no template fields for project {project_id}")

    image_ids = list(detected_boxes.keys())
    image_tasks = await _load_image_tasks_by_image(pool, job_id, image_ids)

    client = get_client()
    semaphore = asyncio.Semaphore(settings.image_concurrency)
    progress_lock = asyncio.Lock()

    async def _bound(image_id: UUID, detections: dict[UUID, dict]) -> None:
        task = image_tasks.get(image_id)
        if task is None:
            log.warning(
                "stage_c.image_task_missing",
                job_id=str(job_id),
                image_id=str(image_id),
            )
            return
        async with semaphore:
            await _process_image(
                pool,
                job=job,
                image_id=image_id,
                detections=detections,
                fields_by_id=fields_by_id,
                image_task=task,
                client=client,
                progress_lock=progress_lock,
            )

    stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(pool, job_id, stop))
    started_at = time.monotonic()
    try:
        await asyncio.gather(*(_bound(img_id, dets) for img_id, dets in detected_boxes.items()))
    finally:
        stop.set()
        try:
            await heartbeat_task
        except Exception:  # noqa: BLE001
            log.exception("stage_c.heartbeat_loop_error", job_id=str(job_id))

    log.info(
        "stage_c.complete",
        job_id=str(job_id),
        project_id=str(project_id),
        image_count=len(detected_boxes),
        elapsed_s=round(time.monotonic() - started_at, 2),
    )
