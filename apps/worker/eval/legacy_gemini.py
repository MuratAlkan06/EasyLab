"""Frozen snapshot of the pre-Phase-7 Gemini Stage B detector (eval-only).

Phase 7 (ADR-007) replaced Gemini detection with the deterministic aligner in
``app.pipeline.align`` and deleted the old detector from ``app``. This module
keeps a byte-for-byte copy of what that detector did, so that
``run_eval.py --backend gemini`` can still reproduce the "before" numbers next
to the aligner's "after" numbers. Do not edit it to track the pipeline: it is a
historical baseline, and nothing in ``app`` imports it.

Frozen from the code removed in Phase 7 Slice 4:

* ``preprocess``: ``image_utils.preprocess_for_stage_b`` (EXIF transpose,
  longest edge 1568 px LANCZOS, JPEG q=88 optimized).
* ``build_prompt``: ``gemini._build_stage_b_prompt``.
* ``call_stage_b``: ``gemini.call_stage_b`` (Gemini 2.5 Pro, temperature 1.0,
  dynamic thinking, 8192 output tokens, native ``response_json_schema``).
* ``DetectedField`` / ``DetectionResponse``: the ``schemas`` models.
* ``convert_box_to_normalized``: ``stage_b.convert_box_to_normalized``.
* ``MODEL``: the old ``settings.gemini_detection_model`` default.

The 2-attempt retry and the acceptance ladder live in ``run_eval.run_gemini``.
``google-genai`` is imported lazily, only when the gemini backend runs.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps
from pydantic import BaseModel

MODEL = "gemini-2.5-pro"
LONGEST_EDGE_PX = 1568
JPEG_QUALITY = 88
MAX_OUTPUT_TOKENS = 8192


# The two models' docstrings are verbatim: Pydantic emits them as schema
# "description"s, i.e. they are part of the response_json_schema sent to Gemini.
class DetectedField(BaseModel):
    """Per-field detection result returned by Stage B.

    `box` is Gemini-native ``[ymin, xmin, ymax, xmax]`` in 0..1000 space.
    Always ``None`` when ``found`` is False; the worker also defensively
    treats a missing/None box on ``found=True`` as not-found.
    """

    field_name: str
    found: bool
    box: list[int] | None = None
    box_confidence: float = 0.0


class DetectionResponse(BaseModel):
    """Stage B response: one DetectedField per template field, in input order."""

    fields: list[DetectedField]


def preprocess(img_bytes: bytes) -> bytes:
    """EXIF transpose -> resize longest edge to 1568 px -> JPEG q=88."""
    with Image.open(io.BytesIO(img_bytes)) as raw:
        raw.load()
        img = ImageOps.exif_transpose(raw)
    width, height = img.size
    longest = max(width, height)
    if longest > LONGEST_EDGE_PX:
        scale = LONGEST_EDGE_PX / longest
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        img = img.resize(new_size, Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def convert_box_to_normalized(box: list[int]) -> dict[str, float]:
    """Convert a Gemini ``[ymin, xmin, ymax, xmax]`` 0..1000 box to normalized 0..1.

    Always clamps to ``[0, 1]``, so out-of-range model output never propagates.
    Negative widths/heights from inverted boxes also clamp to 0, and the ladder
    rejects such boxes via ``is_box_valid``.
    """
    ymin, xmin, ymax, xmax = box
    x = max(0.0, min(1.0, xmin / 1000.0))
    y = max(0.0, min(1.0, ymin / 1000.0))
    width = max(0.0, min(1.0, (xmax - xmin) / 1000.0))
    height = max(0.0, min(1.0, (ymax - ymin) / 1000.0))
    return {"x": x, "y": y, "width": width, "height": height}


def build_prompt(fields: list[dict]) -> str:
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


def make_client(api_key: str):
    """A ``google.genai.Client``. The import happens here, not at module load."""
    from google import genai

    return genai.Client(api_key=api_key)


async def call_stage_b(
    client, model_name: str, image_bytes: bytes, fields: list[dict]
) -> DetectionResponse:
    """Run one detection call. Returns a parsed DetectionResponse.

    ``fields`` items carry ``field_name``, ``semantic_description`` and
    ``reference_box``. ``image_bytes`` is the output of ``preprocess``.
    """
    from google.genai import types

    config = types.GenerateContentConfig(
        temperature=1.0,
        thinking_config=types.ThinkingConfig(thinking_budget=-1),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        response_mime_type="application/json",
        response_json_schema=DetectionResponse.model_json_schema(),
    )

    response = await client.aio.models.generate_content(
        model=model_name,
        contents=[
            build_prompt(fields),
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        ],
        config=config,
    )
    text = response.text
    if not text:
        raise ValueError("Gemini returned empty response for Stage B detection")
    return DetectionResponse.model_validate_json(text)
