"""Guards for ``eval/legacy_gemini.py``, the frozen pre-Phase-7 Gemini detector.

It exists only so ``run_eval.py --backend gemini`` can reproduce the "before"
numbers. These tests pin the parts that make that reproduction faithful (box
conversion, prompt, request config, preprocessing), ``run_eval``'s wiring to
it, and keep ``google-genai`` off the import path of the offline harness. No
network: the client is mocked.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from PIL import Image

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
sys.path.insert(0, str(EVAL_DIR))

import legacy_gemini  # noqa: E402
import run_eval  # noqa: E402  (sets placeholder env before importing app.*)
from gt_io import load_ground_truth  # noqa: E402

_FIELDS = [
    {
        "field_name": "volt",
        "semantic_description": "7-segment LCD, top left",
        "reference_box": {"x": 0.3, "y": 0.3, "width": 0.2, "height": 0.1},
    },
    {"field_name": "amp", "semantic_description": None, "reference_box": None},
]

# ---------------------------------------------------------------------------
# Box coordinate conversion (moved from test_stage_b.py with the function)
# ---------------------------------------------------------------------------


def test_box_conversion_valid() -> None:
    """[ymin=100, xmin=200, ymax=300, xmax=400] → x=0.2 y=0.1 w=0.2 h=0.2."""
    box = legacy_gemini.convert_box_to_normalized([100, 200, 300, 400])
    assert box == {"x": 0.2, "y": 0.1, "width": 0.2, "height": 0.2}


def test_box_conversion_clamps() -> None:
    """Out-of-range values clamp to [0, 1]; underflow clamps to exactly 0."""
    box = legacy_gemini.convert_box_to_normalized([-50, -100, 1500, 1200])
    assert all(0.0 <= v <= 1.0 for v in box.values())
    assert box["x"] == 0.0
    assert box["y"] == 0.0


def test_box_conversion_inverted_clamps_to_zero_size() -> None:
    """Inverted boxes (xmax < xmin) collapse to zero width/height, not negative."""
    box = legacy_gemini.convert_box_to_normalized([400, 600, 200, 300])
    assert box["width"] == 0.0
    assert box["height"] == 0.0


# ---------------------------------------------------------------------------
# Prompt + request: what the old detector sent
# ---------------------------------------------------------------------------


def test_prompt_carries_ref_position_hint_and_falls_back_to_field_name() -> None:
    prompt = legacy_gemini.build_prompt(_FIELDS)
    assert "- volt: 7-segment LCD, top left [ref position: x 30%–50%, y 30%–40% of image]" in prompt
    assert "- amp: amp\n" in prompt


def test_call_stage_b_sends_the_frozen_request_and_parses_the_response() -> None:
    client = MagicMock()
    body = '{"fields": [{"field_name": "volt", "found": true, "box": [1, 2, 3, 4]}]}'
    client.aio.models.generate_content = AsyncMock(return_value=MagicMock(text=body))

    parsed = asyncio.run(legacy_gemini.call_stage_b(client, legacy_gemini.MODEL, b"jpeg", _FIELDS))

    kwargs = client.aio.models.generate_content.await_args.kwargs
    assert kwargs["model"] == "gemini-2.5-pro"
    assert kwargs["contents"][0] == legacy_gemini.build_prompt(_FIELDS)
    config = kwargs["config"]
    assert config.temperature == 1.0
    assert config.thinking_config.thinking_budget == -1
    assert config.max_output_tokens == 8192
    assert config.response_json_schema == legacy_gemini.DetectionResponse.model_json_schema()
    assert parsed.fields[0].box == [1, 2, 3, 4]


def test_preprocess_is_upright_and_capped_at_1568px() -> None:
    raw = Image.new("RGB", (2000, 1000), "white")  # stored landscape
    exif = Image.Exif()
    exif[0x0112] = 6  # displayed portrait
    buf = io.BytesIO()
    raw.save(buf, format="JPEG", exif=exif.tobytes())

    with Image.open(io.BytesIO(legacy_gemini.preprocess(buf.getvalue()))) as out:
        assert out.format == "JPEG"
        assert out.size == (784, 1568)


# ---------------------------------------------------------------------------
# run_eval wiring: the gemini backend runs on the snapshot, not on app code
# ---------------------------------------------------------------------------


def test_run_gemini_drives_the_snapshot_through_the_acceptance_ladder(monkeypatch) -> None:
    """Mocked detector: the first field comes back at its reference box, the
    second is not found. The ladder accepts the first and falls back for the second."""
    full = load_ground_truth(EVAL_DIR / "fixtures" / "ground_truth.json")
    gt = dataclasses.replace(full, targets=full.targets[:1])
    fields = run_eval.template_fields(gt)
    first, second = fields[0], fields[1]
    box = first["reference_box"]
    found = legacy_gemini.DetectedField(
        field_name=first["field_name"],
        found=True,
        box=[
            round(box["y"] * 1000),
            round(box["x"] * 1000),
            round((box["y"] + box["height"]) * 1000),
            round((box["x"] + box["width"]) * 1000),
        ],
        box_confidence=0.9,
    )
    missing = legacy_gemini.DetectedField(field_name=second["field_name"], found=False)
    call = AsyncMock(return_value=legacy_gemini.DetectionResponse(fields=[found, missing]))
    monkeypatch.setattr(legacy_gemini, "make_client", lambda api_key: MagicMock())
    monkeypatch.setattr(legacy_gemini, "call_stage_b", call)

    (result,) = run_eval.run_gemini(gt, fields).values()

    _client, model, image_bytes, sent_fields = call.await_args.args
    assert model == legacy_gemini.MODEL
    assert sent_fields is fields
    assert image_bytes == legacy_gemini.preprocess(gt.path(gt.targets[0]).read_bytes())
    assert result.predictions[first["field_name"]].source == "gemini"
    fallback = result.predictions[second["field_name"]]
    assert (fallback.source, fallback.box) == ("fallback", second["reference_box"])


# ---------------------------------------------------------------------------
# Offline harness stays offline
# ---------------------------------------------------------------------------


def test_importing_the_harness_does_not_import_google_genai() -> None:
    """google-genai is imported only when the gemini backend actually runs."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import legacy_gemini, run_eval; "
        "assert 'google.genai' not in sys.modules, 'google.genai imported'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(EVAL_DIR)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
