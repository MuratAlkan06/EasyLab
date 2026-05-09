"""Gemini client wrapper.

Owns the singleton `genai.Client` and provides per-stage async call helpers.
Only this module talks to the Gemini API; all other pipeline modules call
through the helpers here.
"""

from __future__ import annotations

from google import genai
from google.genai import types

from app.pipeline.schemas import DetectionResponse, TemplateGenerationResponse
from app.settings import settings

_client: genai.Client | None = None


def get_client() -> genai.Client:
    """Return the module-level singleton Gemini client.

    Initialized lazily from `settings.gemini_api_key` so that tests and other
    code paths that don't make API calls don't require a key.
    """
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _build_stage_a_prompt(fields: list[dict]) -> str:
    """Render the Stage A user prompt from the annotated field list."""
    lines = []
    for f in fields:
        box = f["reference_box"]
        lines.append(
            f"- {f['field_name']}: box at "
            f"x={box['x']:.3f}, y={box['y']:.3f}, "
            f"w={box['width']:.3f}, h={box['height']:.3f}"
        )
    field_block = "\n".join(lines)

    return (
        "You are analyzing a reference lab image. I have annotated the following "
        "fields in this image, each identified by a bounding box (x, y, width, "
        "height as fractions of image size, top-left origin):\n\n"
        f"{field_block}\n\n"
        "For each field, produce:\n"
        "- semantic_description: a precise description of WHAT the field shows "
        'visually (e.g. "7-segment LCD display showing voltage reading in '
        'top-left of the instrument panel")\n'
        '- expected_format: your best guess at {"type": "number"|"text"|"boolean", '
        '"unit": "V"|null} — null if unsure\n\n'
        "Return ONLY the fields listed above, in the same order."
    )


async def call_stage_a(
    client: genai.Client,
    model_name: str,
    image_bytes: bytes,
    fields: list[dict],
) -> str:
    """Run a single Stage A generation call. Returns raw `response.text` JSON."""
    config = types.GenerateContentConfig(
        temperature=1.0,
        thinking_config=types.ThinkingConfig(thinking_budget=-1),
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_json_schema=TemplateGenerationResponse.model_json_schema(),
    )

    response = await client.aio.models.generate_content(
        model=model_name,
        contents=[
            _build_stage_a_prompt(fields),
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        ],
        config=config,
    )
    return response.text


def _build_stage_b_prompt(fields: list[dict]) -> str:
    """Render the Stage B user prompt from the field list with semantic descriptions."""
    lines = []
    for f in fields:
        description = f.get("semantic_description") or f["field_name"]
        lines.append(f"- {f['field_name']}: {description}")
    field_block = "\n".join(lines)

    return (
        "You are analyzing a lab image. Locate each of the following fields in "
        "the image. Each field is described by what it shows visually:\n\n"
        f"{field_block}\n\n"
        "For every field, return:\n"
        "- field_name: exactly as listed above\n"
        "- found: true if the field is visible, false otherwise\n"
        "- box: [ymin, xmin, ymax, xmax] in 0..1000 coordinate space (Gemini "
        "native). Set to null when found is false.\n"
        "- box_confidence: 0.0..1.0 detection confidence (0.0 when not found)\n\n"
        "Return one entry per field listed above, in the same order."
    )


async def call_stage_b(
    client: genai.Client,
    model_name: str,
    image_bytes: bytes,
    fields: list[dict],
) -> DetectionResponse:
    """Run a single Stage B generation call. Returns a parsed DetectionResponse.

    `fields` is a list of ``{"field_name": str, "semantic_description": str | None}``.
    """
    config = types.GenerateContentConfig(
        temperature=1.0,
        thinking_config=types.ThinkingConfig(thinking_budget=-1),
        max_output_tokens=1024,
        response_mime_type="application/json",
        response_json_schema=DetectionResponse.model_json_schema(),
    )

    response = await client.aio.models.generate_content(
        model=model_name,
        contents=[
            _build_stage_b_prompt(fields),
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        ],
        config=config,
    )
    return DetectionResponse.model_validate_json(response.text)
