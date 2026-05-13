"""Stage B — Region Detection.

Inputs:
  * Every non-reference image already uploaded for the project.
  * `template_fields` rows (id, field_name, semantic_description) populated
    by Stage A.

Outputs:
  * For each (image, field) pair where the box is missing or rejected, a
    ``cells`` row with status='failed' and ``failure_reason='field_not_visible'``.
  * For each image with at least one valid detected box, the in-memory
    ``DetectedBoxes`` payload returned to the caller (Stage C consumer).
  * `image_tasks.status` advanced to ``extracting`` (any field detected) or
    ``not_found`` (all fields missing).

Runs after Stage A inside `process_batch`. Stage C consumes the returned map.
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

from app.pipeline.crop_utils import box_iou, is_box_valid
from app.pipeline.debug_overlay import OverlayBox, render_overlay
from app.pipeline.gemini import call_stage_b, get_client
from app.pipeline.image_utils import preprocess_for_stage_b
from app.pipeline.schemas import DetectionResponse
from app.settings import settings

log = structlog.get_logger()

# Retry timing for the Gemini call. Two attempts total: an initial call and
# one retry after a short pause (mirrors Stage A behaviour).
_RETRY_WAIT_SECONDS = 2.0
_MAX_ATTEMPTS = 2

# Detections below this box_confidence are treated as too uncertain to crop.
# The reference-box fallback is tried first; failing that, a failed cell is written.
_MIN_BOX_CONFIDENCE = 0.30

# Confidence assigned to reference-box fallback crops. User-drawn boxes on the
# same instrument setup are reliable; 0.55 lets a good Stage C extraction
# (self_confidence ≥ 0.80) reach low_confidence status rather than needs_review.
_REFERENCE_BOX_CONFIDENCE = 0.55

# Heartbeat the parent job at most this often during Stage B. The lock-keeper
# in the worker poll loop watches `heartbeat_at` to detect stuck jobs.
_HEARTBEAT_INTERVAL_SECONDS = 5.0


def convert_box_to_normalized(box: list[int]) -> dict[str, float]:
    """Convert a Gemini ``[ymin, xmin, ymax, xmax]`` 0..1000 box to normalized 0..1.

    Always clamps to ``[0, 1]`` so out-of-range output from the model never
    propagates downstream. Negative widths/heights from inverted boxes also
    clamp to 0; the caller is expected to reject such boxes via ``is_box_valid``.
    """
    ymin, xmin, ymax, xmax = box
    x = max(0.0, min(1.0, xmin / 1000.0))
    y = max(0.0, min(1.0, ymin / 1000.0))
    width = max(0.0, min(1.0, (xmax - xmin) / 1000.0))
    height = max(0.0, min(1.0, (ymax - ymin) / 1000.0))
    return {"x": x, "y": y, "width": width, "height": height}


async def _call_with_retry(
    client, model_name: str, image_bytes: bytes, fields: list[dict]
) -> DetectionResponse:
    """Call Stage B and validate; retry once on any failure (tenacity).

    Raises the underlying exception if both attempts fail so the caller can
    decide how to mark the affected image (all fields → failed cells).
    """
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_fixed(_RETRY_WAIT_SECONDS),
            reraise=True,
        ):
            with attempt:
                return await call_stage_b(client, model_name, image_bytes, fields)
    except RetryError as exc:  # defensive: reraise=True should already unwrap
        raise exc.last_attempt.exception() from exc
    raise RuntimeError("unreachable: AsyncRetrying exited without yielding")


async def _load_template_fields(pool: asyncpg.Pool, project_id) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT id, field_name, semantic_description, reference_box
        FROM template_fields
        WHERE project_id = $1
        ORDER BY display_order
        """,
        project_id,
    )
    fields = []
    for r in rows:
        box = r["reference_box"]
        if isinstance(box, str):
            box = json.loads(box)
        fields.append(
            {
                "id": r["id"],
                "field_name": r["field_name"],
                "semantic_description": r["semantic_description"],
                "reference_box": box,
            }
        )
    return fields


async def _load_image_tasks(pool: asyncpg.Pool, job_id) -> list[dict]:
    """Load all pending image_tasks for the job along with the image metadata.

    Reference images are excluded — only the batch images get a Stage B pass.
    """
    rows = await pool.fetch(
        """
        SELECT t.id          AS task_id,
               t.image_id    AS image_id,
               t.status      AS status,
               i.storage_path AS storage_path,
               i.width_px    AS width_px,
               i.height_px   AS height_px
        FROM image_tasks t
        JOIN images i ON i.id = t.image_id
        WHERE t.job_id = $1
          AND i.is_reference = false
        ORDER BY t.id
        """,
        job_id,
    )
    return [dict(r) for r in rows]


def _download_image_bytes(storage_path: str) -> bytes:
    """Download an image from Supabase Storage (sync supabase-py)."""
    supabase = create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    return supabase.storage.from_(settings.supabase_storage_bucket).download(storage_path)


def _upload_debug_overlay(storage_path: str, jpeg_bytes: bytes) -> None:
    """Upload a STAGE_B_DEBUG overlay JPEG. Sync, called from a thread.

    Upserts so re-running a job within the same debug-storage window doesn't
    error on 'already exists'.
    """
    supabase = create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    supabase.storage.from_(settings.supabase_storage_bucket).upload(
        path=storage_path,
        file=jpeg_bytes,
        file_options={"content-type": "image/jpeg", "upsert": "true"},
    )


def _open_dimensions(img_bytes: bytes) -> tuple[int, int]:
    """Return (width_px, height_px) for the raw image bytes."""
    with Image.open(io.BytesIO(img_bytes)) as img:
        img.load()
        return img.size  # (width, height)


async def _write_failed_cell(
    pool: asyncpg.Pool,
    *,
    job_id: UUID,
    project_id: UUID,
    image_id: UUID,
    field_id: UUID,
    failure_reason: str,
    box_confidence: float = 0.0,
) -> None:
    """Insert a failed cell row for an undetected (or rejected) field.

    Uses the (job_id, image_id, field_id) unique key so retries within the
    same job are idempotent.
    """
    model_versions = json.dumps({"detector": settings.gemini_detection_model})
    await pool.execute(
        """
        INSERT INTO cells (
            job_id, project_id, image_id, field_id,
            box_confidence, value_confidence, combined_confidence,
            status, failure_reason, model_versions
        ) VALUES (
            $1, $2, $3, $4,
            $5, 0, 0,
            'failed', $6, $7::jsonb
        )
        ON CONFLICT (job_id, image_id, field_id) DO UPDATE
        SET status         = excluded.status,
            failure_reason = excluded.failure_reason,
            box_confidence = excluded.box_confidence,
            updated_at     = now()
        """,
        job_id,
        project_id,
        image_id,
        field_id,
        box_confidence,
        failure_reason,
        model_versions,
    )


async def _process_image(
    pool: asyncpg.Pool,
    *,
    job: dict,
    fields: list[dict],
    task: dict,
    client,
    detected: dict[UUID, dict[UUID, dict[str, Any]]],
    progress_lock: asyncio.Lock,
) -> None:
    """Run Stage B for a single image.

    On success, populates ``detected[image_id][field_id]`` with the box dict
    (normalized), box_confidence, and pixel dimensions for Stage C.
    On per-image failure, every field becomes a ``failed`` cell.
    """
    job_id: UUID = job["id"]
    project_id: UUID = job["project_id"]
    image_id: UUID = task["image_id"]
    storage_path: str = task["storage_path"]

    log.info(
        "stage_b.image.start",
        job_id=str(job_id),
        image_id=str(image_id),
    )

    # detecting status — visible to the UI almost immediately
    await pool.execute(
        "UPDATE image_tasks SET status = 'detecting', updated_at = now() WHERE id = $1",
        task["task_id"],
    )

    # Download + measure original dimensions + preprocess for Gemini.
    try:
        raw_bytes = await asyncio.to_thread(_download_image_bytes, storage_path)
        width_px, height_px = await asyncio.to_thread(_open_dimensions, raw_bytes)
        image_bytes = await asyncio.to_thread(preprocess_for_stage_b, raw_bytes)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "stage_b.image.preprocess_failed",
            job_id=str(job_id),
            image_id=str(image_id),
            error=str(exc),
        )
        for field in fields:
            await _write_failed_cell(
                pool,
                job_id=job_id,
                project_id=project_id,
                image_id=image_id,
                field_id=field["id"],
                failure_reason="field_not_visible",
            )
        await pool.execute(
            """
            UPDATE image_tasks
            SET status = 'not_found', error = $2, updated_at = now()
            WHERE id = $1
            """,
            task["task_id"],
            str(exc)[:500],
        )
        return

    # Persist dimensions on the images row if not already set.
    if task.get("width_px") is None or task.get("height_px") is None:
        await pool.execute(
            """
            UPDATE images
            SET width_px   = COALESCE(width_px,  $2),
                height_px  = COALESCE(height_px, $3),
                updated_at = now()
            WHERE id = $1
            """,
            image_id,
            width_px,
            height_px,
        )

    # Declare before the try so the except block can populate it directly.
    image_detected: dict[UUID, dict[str, Any]] = {}

    # Call Gemini with retry. On total failure, try reference-box fallback per
    # field; write a failed cell only when no reference box is available either.
    parsed = None
    try:
        parsed = await _call_with_retry(
            client,
            settings.gemini_detection_model,
            image_bytes,
            [
                {
                    "field_name": f["field_name"],
                    "semantic_description": f["semantic_description"],
                    "reference_box": f.get("reference_box"),
                }
                for f in fields
            ],
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "stage_b.image.gemini_failed",
            job_id=str(job_id),
            image_id=str(image_id),
            error=str(exc),
        )
        for field in fields:
            ref_box = field.get("reference_box")
            if ref_box and is_box_valid(ref_box, width_px, height_px):
                log.info(
                    "stage_b.reference_fallback",
                    job_id=str(job_id),
                    image_id=str(image_id),
                    field_name=field["field_name"],
                    reason="gemini_error",
                )
                image_detected[field["id"]] = {
                    "box": ref_box,
                    "box_confidence": _REFERENCE_BOX_CONFIDENCE,
                    "image_width": width_px,
                    "image_height": height_px,
                }
            else:
                await _write_failed_cell(
                    pool,
                    job_id=job_id,
                    project_id=project_id,
                    image_id=image_id,
                    field_id=field["id"],
                    failure_reason="field_not_visible",
                )
        if not image_detected:
            await pool.execute(
                """
                UPDATE image_tasks
                SET status = 'not_found', error = $2, updated_at = now()
                WHERE id = $1
                """,
                task["task_id"],
                str(exc)[:500],
            )
            return
        # parsed stays None — skip the per-field Gemini loop below.

    # If Gemini succeeded, process each field: apply confidence threshold and
    # fall back to the reference box when the detection is absent or unreliable.
    #
    # ``overlay_boxes`` collects per-field outcomes for STAGE_B_DEBUG so we can
    # render an annotated JPEG after the loop. Order matters for visual reading:
    # the reference annotation goes underneath (dashed), then the accepted/
    # rejected/fallback box on top.
    overlay_boxes: list[OverlayBox] = []
    if parsed is not None:
        by_name = {df.field_name: df for df in parsed.fields}

        for field in fields:
            df = by_name.get(field["field_name"])
            ref_box = field.get("reference_box")

            accepted_box: dict | None = None
            accepted_conf: float = 0.0
            failure_reason: str = "field_not_visible"
            failure_conf: float = 0.0

            # Diagnostics: raw Gemini box (if any) + IoU vs the user's
            # annotation. These are cheap and run regardless of the debug
            # flag because the IoU is useful in normal logs too — it makes
            # the "Gemini is confident about the wrong region" failure mode
            # legible without needing the overlay images.
            raw_box: dict | None = None
            iou_vs_ref: float | None = None
            if df is not None and df.box is not None:
                raw_box = convert_box_to_normalized(df.box)
                if ref_box:
                    iou_vs_ref = round(box_iou(raw_box, ref_box), 4)
                log.info(
                    "stage_b.gemini_raw",
                    job_id=str(job_id),
                    image_id=str(image_id),
                    field_name=field["field_name"],
                    found=bool(df.found),
                    box_confidence=float(df.box_confidence or 0.0),
                    gemini_box_raw=df.box,
                    box_iou_vs_reference=iou_vs_ref,
                )

            if df is None:
                # R1: field name missing from response entirely.
                pass
            elif not df.found or df.box is None:
                # R2: Gemini explicitly did not find the field.
                failure_conf = float(df.box_confidence or 0.0)
            elif (df.box_confidence or 0.0) < _MIN_BOX_CONFIDENCE:
                # R3: detected but below confidence threshold — crop would be unreliable.
                failure_reason = "low_confidence"
                failure_conf = float(df.box_confidence or 0.0)
            else:
                # raw_box was already computed above for the diagnostic log.
                assert raw_box is not None  # narrowed by the elifs
                if not is_box_valid(raw_box, width_px, height_px):
                    # R4: box geometry rejected (too small, degenerate, full-image).
                    failure_conf = float(df.box_confidence or 0.0)
                else:
                    accepted_box = raw_box
                    accepted_conf = float(df.box_confidence or 0.0)

            # Fallback: use the annotated reference box when Gemini detection failed.
            used_fallback = False
            if accepted_box is None and ref_box and is_box_valid(ref_box, width_px, height_px):
                log.info(
                    "stage_b.reference_fallback",
                    job_id=str(job_id),
                    image_id=str(image_id),
                    field_name=field["field_name"],
                    reason=failure_reason,
                )
                accepted_box = ref_box
                accepted_conf = _REFERENCE_BOX_CONFIDENCE
                used_fallback = True

            # Collect overlay entries for the debug-flag JPEG. Always include
            # the user's reference annotation when present so reviewers can
            # see what the model was supposed to find.
            if settings.stage_b_debug:
                if ref_box:
                    overlay_boxes.append(
                        OverlayBox(
                            box=ref_box,
                            label=f"{field['field_name']} (ref)",
                            kind="reference",
                        )
                    )
                if used_fallback and accepted_box is not None:
                    overlay_boxes.append(
                        OverlayBox(
                            box=accepted_box,
                            label=f"{field['field_name']} fallback",
                            kind="fallback",
                            confidence=accepted_conf,
                        )
                    )
                elif accepted_box is not None:
                    overlay_boxes.append(
                        OverlayBox(
                            box=accepted_box,
                            label=field["field_name"],
                            kind="accepted",
                            confidence=accepted_conf,
                        )
                    )
                elif raw_box is not None:
                    overlay_boxes.append(
                        OverlayBox(
                            box=raw_box,
                            label=f"{field['field_name']} rejected",
                            kind="rejected",
                            confidence=failure_conf,
                        )
                    )

            if accepted_box is not None:
                image_detected[field["id"]] = {
                    "box": accepted_box,
                    "box_confidence": accepted_conf,
                    "image_width": width_px,
                    "image_height": height_px,
                }
            else:
                await _write_failed_cell(
                    pool,
                    job_id=job_id,
                    project_id=project_id,
                    image_id=image_id,
                    field_id=field["id"],
                    failure_reason=failure_reason,
                    box_confidence=failure_conf,
                )

    # Emit the overlay JPEG once per image. Best-effort: a Storage outage on
    # the debug path must not fail Stage B itself.
    if settings.stage_b_debug and overlay_boxes:
        try:
            overlay_bytes = await asyncio.to_thread(render_overlay, raw_bytes, overlay_boxes)
            debug_path = f"projects/{project_id}/debug/{job_id}/{image_id}.jpg"
            await asyncio.to_thread(_upload_debug_overlay, debug_path, overlay_bytes)
            log.info(
                "stage_b.debug_overlay_uploaded",
                job_id=str(job_id),
                image_id=str(image_id),
                storage_path=debug_path,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "stage_b.debug_overlay_failed",
                job_id=str(job_id),
                image_id=str(image_id),
                error=str(exc),
            )

    if image_detected:
        detected[image_id] = image_detected
        new_status = "extracting"
    else:
        new_status = "not_found"

    await pool.execute(
        "UPDATE image_tasks SET status = $2, updated_at = now() WHERE id = $1",
        task["task_id"],
        new_status,
    )

    # Bump progress + heartbeat under a lock to keep the counter monotonic.
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
        "stage_b.image.complete",
        job_id=str(job_id),
        image_id=str(image_id),
        detected_count=len(image_detected),
        total_fields=len(fields),
        status=new_status,
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


async def run_stage_b(pool: asyncpg.Pool, job: dict) -> dict[UUID, dict[UUID, dict[str, Any]]]:
    """Run Stage B detection for every non-reference image in the job.

    Returns a nested map ``{image_id: {field_id: {"box", "box_confidence",
    "image_width", "image_height"}}}`` for Stage C consumption. Failed
    detections are written as ``failed`` cells in-line; they are *not*
    included in the returned map.
    """
    project_id = job["project_id"]
    job_id: UUID = job["id"]
    log.info("stage_b.start", job_id=str(job_id), project_id=str(project_id))

    fields = await _load_template_fields(pool, project_id)
    if not fields:
        raise RuntimeError(f"Stage B: no template fields for project {project_id}")

    tasks = await _load_image_tasks(pool, job_id)
    if not tasks:
        log.info("stage_b.no_tasks", job_id=str(job_id))
        return {}

    client = get_client()
    semaphore = asyncio.Semaphore(settings.image_concurrency)
    progress_lock = asyncio.Lock()
    detected: dict[UUID, dict[UUID, dict[str, Any]]] = {}

    async def _bound(task: dict) -> None:
        async with semaphore:
            await _process_image(
                pool,
                job=job,
                fields=fields,
                task=task,
                client=client,
                detected=detected,
                progress_lock=progress_lock,
            )

    stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(pool, job_id, stop))
    started_at = time.monotonic()
    try:
        await asyncio.gather(*(_bound(t) for t in tasks))
    finally:
        stop.set()
        try:
            await heartbeat_task
        except Exception:  # noqa: BLE001
            log.exception("stage_b.heartbeat_loop_error", job_id=str(job_id))

    log.info(
        "stage_b.complete",
        job_id=str(job_id),
        project_id=str(project_id),
        image_count=len(tasks),
        images_with_detections=len(detected),
        elapsed_s=round(time.monotonic() - started_at, 2),
    )
    return detected
