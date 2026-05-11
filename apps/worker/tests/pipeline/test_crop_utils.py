"""Unit tests for crop computation, validation, and encoding."""

from __future__ import annotations

import io

from PIL import Image

from app.pipeline.crop_utils import compute_crop_rect, crop_and_encode, is_box_valid


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
