"""Gemini client wrapper.

Owns the singleton `genai.Client` and provides per-stage async call helpers.
Only this module talks to the Gemini API; all other pipeline modules call
through the helpers here.
"""

from __future__ import annotations

from google import genai
from google.genai import types

from app.pipeline.schemas import (
    DetectionResponse,
    ExtractionResponse,
    TemplateGenerationResponse,
)
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
        "You are analyzing a reference lab instrument image. I have annotated the "
        "following fields, each identified by a bounding box (x, y, width, height "
        "as fractions of image size, top-left origin):\n\n"
        f"{field_block}\n\n"
        "For each field produce:\n"
        "- semantic_description: describe the ACTIVE DISPLAY ELEMENT inside the "
        "box — the part that shows a live, changing reading (e.g. a 7-segment LCD, "
        "a needle gauge, a digital counter). Be specific about its visual appearance "
        "(display type, colour, position relative to surrounding hardware) so that "
        "the same element can be found reliably in other images of the same "
        "instrument.\n"
        "  IMPORTANT: do NOT describe static labels, printed text on the device "
        "body, socket markings, or anything that does not change between images.\n"
        '- expected_format: {"type": "number"|"text"|"boolean", "unit": "V"|null} '
        "— null if unsure\n\n"
        "Return ONLY the fields listed above, in the same order."
    )


async def call_stage_a(
    client: genai.Client,
    model_name: str,
    image_bytes: bytes,
    fields: list[dict],
):
    """Run a single Stage A generation call.

    Returns the raw Gemini response so callers can read both `.text` (the JSON
    body) and `.usage_metadata.total_token_count` for spend-control accounting.
    """
    config = types.GenerateContentConfig(
        temperature=1.0,
        thinking_config=types.ThinkingConfig(thinking_budget=-1),
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_json_schema=TemplateGenerationResponse.model_json_schema(),
    )

    return await client.aio.models.generate_content(
        model=model_name,
        contents=[
            _build_stage_a_prompt(fields),
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        ],
        config=config,
    )


def _build_stage_b_prompt(fields: list[dict]) -> str:
    """Render the Stage B user prompt from the field list with semantic descriptions."""
    lines = []
    for f in fields:
        description = f.get("semantic_description") or f["field_name"]
        ref_box = f.get("reference_box")
        if ref_box:
            x_lo = round(ref_box["x"] * 100)
            x_hi = round((ref_box["x"] + ref_box["width"]) * 100)
            y_lo = round(ref_box["y"] * 100)
            y_hi = round((ref_box["y"] + ref_box["height"]) * 100)
            spatial = f" [ref position: x {x_lo}%–{x_hi}%, y {y_lo}%–{y_hi}% of image]"
        else:
            spatial = ""
        lines.append(f"- {f['field_name']}: {description}{spatial}")
    field_block = "\n".join(lines)

    return (
        "You are analyzing a lab instrument image. Locate each active display "
        "element described below. Each description refers specifically to a live "
        "readout (LCD segment, digital display, gauge needle, etc.) — NOT to any "
        "static label, printed text, or socket marking on the device body.\n\n"
        f"{field_block}\n\n"
        "The [ref position] hint shows approximately where this display appeared "
        "in a reference image of the same instrument. The current image may have "
        "slight position/zoom differences, but the display should be near that region.\n\n"
        "Rules:\n"
        "- Your box must tightly enclose ONLY the active display/readout element "
        "itself — the region that shows a changing numeric or text value.\n"
        "- Do NOT box static text printed on the device (e.g. 'AMP', 'VOLT', "
        "terminal labels, range markings, socket labels).\n"
        "- Use the [ref position] hint to anchor your search — if the hint points "
        "to a region and you see an active display there, that is almost certainly "
        "the correct element even if your confidence would otherwise be low.\n"
        "- If you are genuinely uncertain whether a region is a live display or a "
        "static label, return it with a low box_confidence (≤0.3) rather than "
        "defaulting to found=false — a low-confidence detection is more useful than "
        "no detection.\n"
        "- Only set found=false when the display element is clearly absent from the "
        "image entirely (e.g. cropped out, covered, or this instrument does not "
        "have that field at all).\n"
        "- If the display is present but partially obscured or at an angle, still "
        "return it with an appropriate box_confidence.\n\n"
        "For every field return:\n"
        "- field_name: exactly as listed above\n"
        "- found: true only if you can locate the active display element\n"
        "- box: [ymin, xmin, ymax, xmax] in 0..1000 coordinate space (Gemini "
        "native). null when found is false.\n"
        "- box_confidence: 0.0..1.0 — your confidence that the box encloses the "
        "correct live readout (0.0 when not found)\n\n"
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
        max_output_tokens=8192,
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
    text = response.text
    if not text:
        raise ValueError("Gemini returned empty response for Stage B detection")
    return DetectionResponse.model_validate_json(text)


def _build_stage_c_prompt(field: dict) -> str:
    """Render the Stage C user prompt for a single cropped field.

    ``field`` is ``{"field_name": str, "expected_format": dict | None}``.
    ``expected_format`` follows the Stage A schema:
    ``{"type": "number"|"text"|"boolean", "unit": str | None}``.
    """
    field_name = field["field_name"]
    expected_format = field.get("expected_format") or {}

    hint_lines = []
    expected_type = expected_format.get("type")
    expected_unit = expected_format.get("unit")
    if expected_type:
        hint_lines.append(f"- expected type: {expected_type}")
    if expected_unit:
        hint_lines.append(f"- expected unit: {expected_unit}")
    hint_block = "\n".join(hint_lines)
    if hint_block:
        hint_block = f"\nHints for this field:\n{hint_block}\n"

    return (
        "You are reading a single field cropped from a lab instrument image. "
        f"The field is labelled '{field_name}'.{hint_block}\n"
        "The crop should show a live display readout (LCD digits, gauge needle, "
        "digital counter, etc.).\n\n"
        "Return:\n"
        "- raw_text: exactly what appears in the crop, character-for-character. "
        "If the crop shows only static printed labels or device markings rather "
        "than a live reading, write what you see literally.\n"
        "- parsed_value: the typed value (number/string/boolean) or null if you "
        "cannot read a live value.\n"
        "- unit_seen: the unit string as it appears in the crop, or null if none.\n"
        "- legible: true if the crop shows a readable live display value. Set "
        "false if the crop is blurry, obscured, or appears to show only static "
        "device labels/markings rather than an active readout.\n"
        "- self_confidence: your confidence that the crop contains a correct live "
        "reading, 0.0..1.0. Use a low value (≤0.3) if the crop looks like a "
        "label or terminal marking rather than a live display.\n"
    )


async def call_stage_c(
    client: genai.Client,
    model_name: str,
    crop_bytes: bytes,
    field: dict,
) -> ExtractionResponse:
    """Run a single Stage C generation call. Returns a parsed ExtractionResponse.

    ``crop_bytes`` is a JPEG crop (≤768 px on the longest edge per
    docs/ai-pipeline.md §5). ``field`` is
    ``{"field_name": str, "expected_format": dict | None}``.
    """
    config = types.GenerateContentConfig(
        temperature=0.0,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        max_output_tokens=1024,
        response_mime_type="application/json",
        response_json_schema=ExtractionResponse.model_json_schema(),
    )

    response = await client.aio.models.generate_content(
        model=model_name,
        contents=[
            _build_stage_c_prompt(field),
            types.Part.from_bytes(data=crop_bytes, mime_type="image/jpeg"),
        ],
        config=config,
    )
    text = response.text
    if not text:
        raise ValueError("Gemini returned empty response for Stage C extraction")
    return ExtractionResponse.model_validate_json(text)
