"""Stage A — Template Generation.

Inputs:
  * The reference image stored in Supabase Storage.
  * The set of `template_fields` annotated by the user (one box per field).

Outputs:
  * `template_fields.semantic_description` and `template_fields.expected_format`
    populated for every field in the project.
  * `projects.template_quality = 'degraded'` if both Gemini attempts failed and
    we had to fall back to synthesized descriptions.

Runs once per project at the start of `process_batch`. Stages B+C consume the
written `semantic_description` values in later phases.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import structlog
from supabase import create_client
from tenacity import (
    AsyncRetrying,
    RetryError,
    stop_after_attempt,
    wait_fixed,
)

from app.pipeline import spend_controls
from app.pipeline.gemini import call_stage_a, get_client
from app.pipeline.image_utils import preprocess_for_stage_a
from app.pipeline.schemas import TemplateGenerationResponse
from app.settings import settings

log = structlog.get_logger()

# Retry timing for the Gemini call. We attempt twice in total: an initial call
# and one retry after a short pause. Anything beyond that triggers fallback.
_RETRY_WAIT_SECONDS = 2.0
_MAX_ATTEMPTS = 2


def _position_label(x: float, y: float) -> str:
    """Map a normalized box origin to a coarse human position label."""
    if x < 0.4 and y < 0.4:
        return "top-left"
    if x >= 0.6 and y < 0.4:
        return "top-right"
    if x < 0.4:
        return "bottom-left"
    if x >= 0.6:
        return "bottom-right"
    return "center"


def _synthesize_fallback(field: dict) -> str:
    """Synthesize a degraded semantic description from box position only."""
    box = field["reference_box"]
    pos = _position_label(box["x"], box["y"])
    return f"field named '{field['field_name']}' at {pos} of image"


async def _call_with_retry(
    client, model_name: str, image_bytes: bytes, fields: list[dict]
) -> tuple[TemplateGenerationResponse, int]:
    """Call Stage A and validate; retry once on any failure (tenacity).

    Returns the parsed response and the total Gemini token count for spend
    accounting. Raises the underlying exception if both attempts fail so the
    caller can fall back to synthesized descriptions.
    """
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_fixed(_RETRY_WAIT_SECONDS),
            reraise=True,
        ):
            with attempt:
                response = await call_stage_a(client, model_name, image_bytes, fields)
                parsed = TemplateGenerationResponse.model_validate_json(response.text)
                tokens = spend_controls.extract_total_tokens(response)
                return parsed, tokens
    except RetryError as exc:  # defensive: reraise=True should already unwrap
        raise exc.last_attempt.exception() from exc
    raise RuntimeError("unreachable: AsyncRetrying exited without yielding")


async def _load_fields(pool: asyncpg.Pool, project_id) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT id, field_name, reference_box
        FROM template_fields
        WHERE project_id = $1
        ORDER BY display_order
        """,
        project_id,
    )
    fields: list[dict] = []
    for r in rows:
        box = r["reference_box"]
        # asyncpg returns jsonb as str by default unless a codec is registered.
        if isinstance(box, str):
            box = json.loads(box)
        fields.append(
            {
                "id": r["id"],
                "field_name": r["field_name"],
                "reference_box": box,
            }
        )
    return fields


async def _load_reference_storage_path(pool: asyncpg.Pool, project_id) -> str:
    row = await pool.fetchrow(
        """
        SELECT i.storage_path
        FROM projects p
        JOIN images i ON i.id = p.reference_image_id
        WHERE p.id = $1
        """,
        project_id,
    )
    if row is None:
        raise RuntimeError(f"Stage A: no reference image set for project {project_id}")
    return row["storage_path"]


def _download_reference_bytes(storage_path: str) -> bytes:
    """Download the reference image from Supabase Storage (sync supabase-py)."""
    supabase = create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    return supabase.storage.from_(settings.supabase_storage_bucket).download(storage_path)


async def _workspace_id_for_project(pool: asyncpg.Pool, project_id):
    row = await pool.fetchrow(
        "SELECT workspace_id FROM projects WHERE id = $1",
        project_id,
    )
    return row["workspace_id"] if row else None


async def run_stage_a(pool: asyncpg.Pool, job: dict) -> None:
    project_id = job["project_id"]
    log.info("stage_a.start", project_id=str(project_id))

    fields = await _load_fields(pool, project_id)
    if not fields:
        raise RuntimeError(f"Stage A: no template fields for project {project_id}")

    storage_path = await _load_reference_storage_path(pool, project_id)
    workspace_id = await _workspace_id_for_project(pool, project_id)

    # Storage download and Pillow ops are blocking; offload to a thread.
    raw_bytes = await asyncio.to_thread(_download_reference_bytes, storage_path)
    image_bytes = await asyncio.to_thread(preprocess_for_stage_a, raw_bytes)

    client = get_client()

    degraded = False
    descriptions_by_name: dict[str, tuple[str, dict | None]] = {}
    try:
        parsed, tokens_used = await _call_with_retry(
            client, settings.gemini_model_pro, image_bytes, fields
        )
        spend_controls.record_success()
        if workspace_id is not None:
            await spend_controls.record_token_usage(pool, workspace_id, tokens_used)
        for ft in parsed.fields:
            descriptions_by_name[ft.field_name] = (
                ft.semantic_description,
                ft.expected_format,
            )
    except Exception as exc:  # noqa: BLE001
        tripped = spend_controls.record_failure(exc)
        log.error(
            "stage_a.degraded_fallback",
            project_id=str(project_id),
            error=str(exc),
            breaker_tripped=tripped,
        )
        if tripped:
            # Pause this job; the loop will pick it back up after the cooldown.
            await spend_controls.pause_job(pool, job["id"])
            raise
        degraded = True

    # Write per-field results. Any field missing from the model response (or
    # all of them if degraded) gets a synthesized description.
    for field in fields:
        if field["field_name"] in descriptions_by_name:
            description, expected_format = descriptions_by_name[field["field_name"]]
        else:
            description = _synthesize_fallback(field)
            expected_format = None
            degraded = degraded or True

        expected_format_json = json.dumps(expected_format) if expected_format is not None else None
        await pool.execute(
            """
            UPDATE template_fields
            SET semantic_description = $1,
                expected_format      = $2::jsonb,
                updated_at           = now()
            WHERE id = $3
            """,
            description,
            expected_format_json,
            field["id"],
        )

    if degraded:
        await pool.execute(
            "UPDATE projects SET template_quality = 'degraded', updated_at = now() WHERE id = $1",
            project_id,
        )

    log.info(
        "stage_a.complete",
        project_id=str(project_id),
        field_count=len(fields),
        degraded=degraded,
    )
