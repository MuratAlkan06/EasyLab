"""EXIF upright-space canonicalization (ADR-007 Slice 2).

The pipeline has one coordinate space: the upright image (EXIF orientation
applied). These tests pin it for every consumer of image bytes: Stage B's
dimension measurement, Stage C's crop canvas, and the debug overlay renderer.

Fixtures are Pillow-generated in memory. An upright "scene" is stored as the
raw pixel grid a camera would write for each orientation, tagged with that
orientation, so an EXIF-aware reader turns it back into the scene. PNG
fixtures are lossless and allow byte-exact comparisons; JPEG fixtures cover the
format phones actually produce.
"""

from __future__ import annotations

import asyncio
import io
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from PIL import Image, ImageChops, ImageStat

from app.pipeline import stage_b, stage_c
from app.pipeline.align import AlignedBox, AlignResult
from app.pipeline.crop_utils import compute_crop_rect, crop_and_encode
from app.pipeline.debug_overlay import OverlayBox, render_overlay
from app.pipeline.image_utils import open_upright, upright_size

_ORIENTATION_TAG = 0x0112

# Portrait upright scene, like a phone photo held upright. Every edge is a
# multiple of 16 so JPEG chroma blocks line up in every orientation.
_UPRIGHT_W, _UPRIGHT_H = 192, 288
_RED_BLOCK = (32, 48, 112, 96)  # upright-space pixel rect, top-left area
_BLUE_BLOCK = (128, 192, 176, 272)  # upright-space pixel rect, bottom-right area
# Normalized upright-space box hugging the red block.
_RED_BOX = {
    "x": 32 / _UPRIGHT_W,
    "y": 48 / _UPRIGHT_H,
    "width": 80 / _UPRIGHT_W,
    "height": 48 / _UPRIGHT_H,
}

# Raw stored grid for each EXIF orientation, per the EXIF spec: the inverse of
# the transpose an EXIF-aware reader applies to display the image upright.
_RAW_FROM_UPRIGHT: dict[int, Image.Transpose | None] = {
    1: None,
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_90,  # stored rotated 90° CCW; display rotates it CW
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_270,
}


def _upright_scene() -> Image.Image:
    img = Image.new("RGB", (_UPRIGHT_W, _UPRIGHT_H), (24, 24, 24))
    img.paste((220, 32, 32), _RED_BLOCK)
    img.paste((32, 32, 220), _BLUE_BLOCK)
    return img


def _encode(img: Image.Image, fmt: str, orientation: int | None) -> bytes:
    kwargs: dict = {"quality": 95} if fmt == "JPEG" else {}
    if orientation is not None:
        exif = Image.Exif()
        exif[_ORIENTATION_TAG] = orientation
        kwargs["exif"] = exif.tobytes()
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def _tagged(orientation: int, fmt: str = "PNG") -> bytes:
    """The upright scene stored as a camera would for ``orientation``, tagged."""
    method = _RAW_FROM_UPRIGHT[orientation]
    raw = _upright_scene() if method is None else _upright_scene().transpose(method)
    return _encode(raw, fmt, orientation)


def _upright_baked(fmt: str = "PNG") -> bytes:
    """The upright scene with the rotation baked into the pixels, no EXIF."""
    return _encode(_upright_scene(), fmt, None)


def _raw_size(img_bytes: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(img_bytes)) as img:
        return img.size


def _same_pixels(a: Image.Image, b: Image.Image) -> bool:
    return a.size == b.size and ImageChops.difference(a, b).getbbox() is None


def _mean_abs_diff(a: Image.Image, b: Image.Image) -> float:
    return max(ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB"))).mean)


def _stage_c_crop(img_bytes: bytes, box: dict) -> bytes:
    """Stage B measures dims, Stage C crops: the production data flow."""
    width, height = stage_b._open_dimensions(img_bytes)
    rect = compute_crop_rect(box, width, height)
    return crop_and_encode(stage_c._open_image(img_bytes), rect)


# ---------------------------------------------------------------------------
# image_utils helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("orientation", range(1, 9))
def test_open_upright_returns_the_upright_scene(orientation: int) -> None:
    upright = open_upright(_tagged(orientation))
    assert _same_pixels(upright.convert("RGB"), _upright_scene())


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
@pytest.mark.parametrize("orientation", [None, *range(1, 9)])
def test_upright_size_matches_open_upright(fmt: str, orientation: int | None) -> None:
    img_bytes = _upright_baked(fmt) if orientation is None else _tagged(orientation, fmt)
    assert upright_size(img_bytes) == open_upright(img_bytes).size == (_UPRIGHT_W, _UPRIGHT_H)


# ---------------------------------------------------------------------------
# (a) Stage B measures upright dimensions
# ---------------------------------------------------------------------------


def test_stage_b_orientation_6_dimensions_are_transposed_raw_dimensions() -> None:
    img_bytes = _tagged(6, "JPEG")
    raw_w, raw_h = _raw_size(img_bytes)
    assert (raw_w, raw_h) == (_UPRIGHT_H, _UPRIGHT_W)  # stored landscape
    assert stage_b._open_dimensions(img_bytes) == (raw_h, raw_w)


@pytest.mark.asyncio
async def test_stage_b_backfills_upright_dimensions_and_hands_them_to_stage_c() -> None:
    """The lazy images.width_px/height_px backfill and the Stage C payload both
    carry upright dims for an orientation-6 photo."""
    pool = AsyncMock()
    task = {
        "task_id": uuid4(),
        "image_id": uuid4(),
        "status": "pending",
        "storage_path": "projects/x/originals/y.jpg",
        "width_px": None,
        "height_px": None,
    }
    field = {"id": uuid4(), "field_name": "Voltage", "semantic_description": "d"}
    box = {"x": 0.2, "y": 0.1, "width": 0.2, "height": 0.2}
    response = AlignResult(
        fields={field["id"]: AlignedBox(box, 0.85, "homography")},
        h_status="strong",
        matches=100,
        inliers=90,
        homography=None,
    )
    detected: dict = {}

    with (
        patch.object(stage_b, "_download_image_bytes", return_value=_tagged(6, "JPEG")),
        patch.object(stage_b, "align_target", return_value=response),
    ):
        await stage_b._process_image(
            pool,
            job={"id": uuid4(), "project_id": uuid4()},
            fields=[field],
            task=task,
            reference=object(),
            detected=detected,
            progress_lock=asyncio.Lock(),
        )

    backfill = [c.args for c in pool.execute.await_args_list if "UPDATE images" in c.args[0]]
    assert len(backfill) == 1
    assert backfill[0][1:] == (task["image_id"], _UPRIGHT_W, _UPRIGHT_H)
    payload = detected[task["image_id"]][field["id"]]
    assert (payload["image_width"], payload["image_height"]) == (_UPRIGHT_W, _UPRIGHT_H)


# ---------------------------------------------------------------------------
# (b) Stage C crops the upright region
# ---------------------------------------------------------------------------


def test_orientation_6_crop_equals_crop_from_pre_rotated_copy() -> None:
    assert _stage_c_crop(_tagged(6), _RED_BOX) == _stage_c_crop(_upright_baked(), _RED_BOX)


def test_orientation_6_jpeg_crop_lands_on_the_upright_region() -> None:
    """Same check on real JPEG phone-style bytes (lossy, so compared by pixels)."""
    got = Image.open(io.BytesIO(_stage_c_crop(_tagged(6, "JPEG"), _RED_BOX)))
    want = Image.open(io.BytesIO(_stage_c_crop(_upright_baked("JPEG"), _RED_BOX)))
    assert got.size == want.size
    assert _mean_abs_diff(got, want) < 4.0


# ---------------------------------------------------------------------------
# (c) Orientation-1 images behave exactly as before (raw grid == upright grid)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("orientation", [None, 1])
def test_orientation_1_is_byte_identical_to_raw_decode(orientation: int | None) -> None:
    img_bytes = _encode(_upright_scene(), "JPEG", orientation)
    with Image.open(io.BytesIO(img_bytes)) as legacy:  # the pre-Slice-2 decode
        legacy.load()
        canvas = stage_c._open_image(img_bytes)
        assert canvas.mode == legacy.mode
        assert _same_pixels(canvas, legacy)
        assert stage_b._open_dimensions(img_bytes) == legacy.size

        width, height = legacy.size
        rect = compute_crop_rect(_RED_BOX, width, height)
        assert crop_and_encode(canvas, rect) == crop_and_encode(legacy, rect)

    # An explicit Orientation=1 tag renders exactly like no tag at all.
    boxes = [OverlayBox(box=_RED_BOX, label="v", kind="accepted", confidence=0.9)]
    untagged = _encode(_upright_scene(), "JPEG", None)
    assert render_overlay(img_bytes, boxes) == render_overlay(untagged, boxes)


# ---------------------------------------------------------------------------
# (d) The debug overlay is drawn on the upright canvas
# ---------------------------------------------------------------------------


def test_overlay_canvas_is_upright_for_orientation_6() -> None:
    boxes = [
        OverlayBox(box=_RED_BOX, label="volt (ref)", kind="reference"),
        OverlayBox(box=_RED_BOX, label="volt", kind="accepted", confidence=0.9),
    ]
    out = render_overlay(_tagged(6), boxes)

    # Byte-identical to drawing on a copy with the rotation baked in.
    assert out == render_overlay(_upright_baked(), boxes)
    with Image.open(io.BytesIO(out)) as decoded:
        assert decoded.size == (_UPRIGHT_W, _UPRIGHT_H)
        # No orientation tag survives, so viewers won't rotate it a second time.
        assert _ORIENTATION_TAG not in decoded.getexif()


def test_overlay_canvas_is_upright_for_orientation_6_jpeg() -> None:
    out = render_overlay(_tagged(6, "JPEG"), [])
    with Image.open(io.BytesIO(out)) as decoded:
        decoded.load()
        assert decoded.size == (_UPRIGHT_W, _UPRIGHT_H)
        assert _ORIENTATION_TAG not in decoded.getexif()
        assert _mean_abs_diff(decoded, _upright_scene()) < 4.0
