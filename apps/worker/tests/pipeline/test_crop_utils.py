"""Unit tests for crop computation, validation, and encoding."""

from __future__ import annotations

import io

from PIL import Image

from app.pipeline.crop_utils import box_iou, compute_crop_rect, crop_and_encode, is_box_valid


def test_compute_crop_rect_basic() -> None:
    # Box centered in a 1000x800 image: x=0.4..0.6 (200px wide), y=0.4..0.6 (160px tall).
    box = {"x": 0.4, "y": 0.4, "width": 0.2, "height": 0.2}
    left, upper, right, lower = compute_crop_rect(box, 1000, 800, padding_pct=0.08)

    # Without padding the rect would be (400, 320, 600, 480).
    # 8% padding adds 16px on each side horizontally, ~12.8px (=13) vertically.
    assert left == 400 - 16
    assert right == 600 + 16
    assert upper == 320 - round(160 * 0.08)
    assert lower == 480 + round(160 * 0.08)


def test_compute_crop_rect_clamps_to_zero() -> None:
    # Box hugging the top-left corner — padding would push left/upper negative.
    box = {"x": 0.0, "y": 0.0, "width": 0.1, "height": 0.1}
    left, upper, _right, _lower = compute_crop_rect(box, 1000, 800)
    assert left == 0
    assert upper == 0


def test_compute_crop_rect_clamps_to_image_bounds() -> None:
    # Box hugging the bottom-right corner — padding would push right/lower past the image.
    box = {"x": 0.9, "y": 0.9, "width": 0.1, "height": 0.1}
    _left, _upper, right, lower = compute_crop_rect(box, 1000, 800)
    assert right == 1000
    assert lower == 800


def test_is_box_valid_normal() -> None:
    box = {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}
    assert is_box_valid(box, 1000, 800) is True


def test_is_box_invalid_tiny_area() -> None:
    # 4x5 px box on a 1000x800 image: area = 20 < 25.
    box = {"x": 0.0, "y": 0.0, "width": 4 / 1000, "height": 5 / 800}
    assert is_box_valid(box, 1000, 800) is False


def test_is_box_invalid_narrow() -> None:
    # 5x100 px box on a 1000x800 image: width 5 < 8.
    box = {"x": 0.0, "y": 0.0, "width": 5 / 1000, "height": 100 / 800}
    assert is_box_valid(box, 1000, 800) is False


def test_is_box_invalid_full_coverage() -> None:
    # Box covers ~90% of the image — over the 60% ceiling.
    box = {"x": 0.05, "y": 0.05, "width": 0.95, "height": 0.95}
    assert is_box_valid(box, 1000, 800) is False


def test_crop_and_encode_returns_jpeg() -> None:
    img = Image.new("RGB", (200, 100), color=(120, 200, 90))
    out = crop_and_encode(img, (10, 10, 110, 90))
    assert out[:3] == b"\xff\xd8\xff"


def test_crop_and_encode_respects_max_edge() -> None:
    img = Image.new("RGB", (2000, 2000), color=(255, 0, 0))
    # Crop a 1500x1500 region — still well above the 768 cap.
    out = crop_and_encode(img, (250, 250, 1750, 1750), max_longest_edge=768)
    with Image.open(io.BytesIO(out)) as decoded:
        decoded.load()
        assert max(decoded.size) <= 768


# ---------------------------------------------------------------------------
# box_iou — used by Stage B diagnostics to detect "Gemini is confident about
# the wrong region" (high box_confidence + low IoU vs the user's annotation).
# ---------------------------------------------------------------------------


def test_box_iou_identical_boxes() -> None:
    box = {"x": 0.2, "y": 0.3, "width": 0.4, "height": 0.2}
    assert box_iou(box, box) == 1.0


def test_box_iou_disjoint_boxes() -> None:
    a = {"x": 0.0, "y": 0.0, "width": 0.2, "height": 0.2}
    b = {"x": 0.5, "y": 0.5, "width": 0.2, "height": 0.2}
    assert box_iou(a, b) == 0.0


def test_box_iou_one_inside_the_other() -> None:
    outer = {"x": 0.0, "y": 0.0, "width": 0.4, "height": 0.4}  # area 0.16
    inner = {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}  # area 0.04
    # intersection = inner = 0.04; union = outer = 0.16; IoU = 0.25
    assert abs(box_iou(outer, inner) - 0.25) < 1e-9


def test_box_iou_partial_overlap() -> None:
    a = {"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5}  # area 0.25
    b = {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5}  # area 0.25
    # overlap is 0.25 x 0.25 = 0.0625; union = 0.25 + 0.25 - 0.0625 = 0.4375
    assert abs(box_iou(a, b) - (0.0625 / 0.4375)) < 1e-9


def test_box_iou_zero_area_box() -> None:
    a = {"x": 0.2, "y": 0.2, "width": 0.0, "height": 0.3}
    b = {"x": 0.2, "y": 0.2, "width": 0.4, "height": 0.4}
    assert box_iou(a, b) == 0.0
