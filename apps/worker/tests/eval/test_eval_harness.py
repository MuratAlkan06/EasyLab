"""Guards for the offline Stage B eval harness in ``apps/worker/eval/``.

The harness scripts themselves are not collected by pytest; these tests import
them to pin the two things later slices rely on:

* the EXIF raw->upright box mapping (Slice 2's acceptance reads the exif6 row),
* the committed fixtures/ground truth stay valid and metadata-free (privacy).

Deliberately numpy-free: tests/pipeline/test_paddle_ocr.py only installs its
numpy shim when numpy has not been imported yet, so importing it here (this
package is collected first) would change that test's behaviour.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageOps

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
sys.path.insert(0, str(EVAL_DIR))

import run_eval  # noqa: E402  (sets placeholder env before importing app.*)
from gt_io import load_ground_truth  # noqa: E402

GT_PATH = EVAL_DIR / "fixtures" / "ground_truth.json"


@pytest.mark.parametrize("orientation", [2, 3, 4, 5, 6, 7, 8])
def test_to_upright_matches_pillow_exif_transpose(orientation: int) -> None:
    upright = Image.new("RGB", (300, 200), "black")
    upright.paste((255, 0, 0), (30, 20, 90, 60))
    exif = Image.Exif()
    exif[0x0112] = orientation
    # Find the stored (raw) pixels that an EXIF-aware reader turns into `upright`.
    for candidate in Image.Transpose:
        raw = upright.transpose(candidate)
        buf = io.BytesIO()
        raw.save(buf, format="PNG", exif=exif)
        buf.seek(0)
        shown = ImageOps.exif_transpose(Image.open(buf)).convert("RGB")
        if shown.size == upright.size and ImageChops.difference(shown, upright).getbbox() is None:
            break
    else:  # pragma: no cover
        pytest.fail("no transpose reproduces the upright image")

    left, top, right, bottom = raw.getbbox()  # the red block is the only non-black region
    w, h = raw.size
    raw_box = {
        "x": left / w,
        "y": top / h,
        "width": (right - left) / w,
        "height": (bottom - top) / h,
    }
    mapped = run_eval.to_upright(raw_box, run_eval.CanvasInfo(orientation, "raw"))
    expected = {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}
    assert run_eval.box_iou(mapped, expected) == pytest.approx(1.0)


def test_committed_ground_truth_is_valid() -> None:
    gt = load_ground_truth(GT_PATH)  # raises on schema/derivation problems
    assert gt.reference.file == "reference.jpg"
    assert {t.cls for t in gt.targets} == {"aligned", "shifted", "exif6", "lighting"}
    assert 8 <= len(gt.targets) <= 12


def test_committed_fixtures_carry_no_metadata() -> None:
    total = 0
    for path in sorted((EVAL_DIR / "fixtures").glob("*.jpg")):
        total += path.stat().st_size
        with Image.open(path) as im:
            assert max(im.size) <= 1600, path.name
            assert "icc_profile" not in im.info and "comment" not in im.info, path.name
            exif = dict(im.getexif())
        # Only the synthesized exif6 fixture may carry EXIF, and only Orientation.
        assert exif in ({}, {0x0112: 6}), f"{path.name}: {exif}"
    assert total < 5_000_000


def test_exif6_scores_like_its_source_once_stage_c_is_upright(monkeypatch) -> None:
    """Slice 2 contract: with an upright Stage C canvas, the exif6 fixture must
    score exactly like the fixture it was derived from."""
    monkeypatch.setattr(
        run_eval,
        "_stage_c_canvas",
        lambda b: ImageOps.exif_transpose(Image.open(io.BytesIO(b))),
    )
    gt = load_ground_truth(GT_PATH)
    fields = run_eval.template_fields(gt)
    results = run_eval.run_reference(gt, fields)
    pairs, canvases, _ = run_eval.score(gt, fields, results, 0.5)

    exif6 = next(t for t in gt.targets if t.cls == "exif6")
    assert canvases[exif6.file].space == "upright"

    def ious(image: str) -> dict[str, float]:
        return {p.field: p.iou for p in pairs if p.image == image}

    assert ious(exif6.file) == ious(exif6.derived_from)
