"""Crop computation, validation, and encoding for review-table thumbnails.

Detected boxes from Stage B arrive as normalized 0..1 coordinates
(``x``, ``y``, ``width``, ``height``) against the original image dimensions.
This module converts them to pixel rectangles, applies padding, rejects
clearly bad boxes, and encodes the resulting crop as JPEG.
"""

from __future__ import annotations

from PIL import Image

from app.pipeline.image_utils import encode_jpeg, resize_longest_edge

# Crop quality knobs — tuned for the review-table thumbnail use case.
DEFAULT_PADDING_PCT = 0.08
DEFAULT_MAX_LONGEST_EDGE = 768
DEFAULT_JPEG_QUALITY = 92

# Box-rejection thresholds.
_MIN_AREA_PX = 25
_MIN_DIMENSION_PX = 8
_MAX_COVERAGE_RATIO = 0.60


def compute_crop_rect(
    detected_box: dict,
    image_width: int,
    image_height: int,
    padding_pct: float = DEFAULT_PADDING_PCT,
) -> tuple[int, int, int, int]:
    """Convert a normalized box to a padded pixel rect ``(left, upper, right, lower)``.

    Padding is computed as ``padding_pct`` of the box's pixel width/height and
    added on each side. The result is clamped to the image bounds.
    """
    x_px = detected_box["x"] * image_width
    y_px = detected_box["y"] * image_height
    w_px = detected_box["width"] * image_width
    h_px = detected_box["height"] * image_height

    pad_x = w_px * padding_pct
    pad_y = h_px * padding_pct

    left = max(0, round(x_px - pad_x))
    upper = max(0, round(y_px - pad_y))
    right = min(image_width, round(x_px + w_px + pad_x))
    lower = min(image_height, round(y_px + h_px + pad_y))
    return left, upper, right, lower


def is_box_valid(
    detected_box: dict,
    image_width: int,
    image_height: int,
) -> bool:
    """Return False if the detected box is too small, too thin, or covers too much."""
    pixel_width = detected_box["width"] * image_width
    pixel_height = detected_box["height"] * image_height
    image_area = image_width * image_height

    if pixel_width * pixel_height < _MIN_AREA_PX:
        return False
    if pixel_width < _MIN_DIMENSION_PX or pixel_height < _MIN_DIMENSION_PX:
        return False
    if image_area > 0 and (pixel_width * pixel_height) / image_area > _MAX_COVERAGE_RATIO:
        return False
    return True


def crop_and_encode(
    img: Image.Image,
    crop_rect: tuple[int, int, int, int],
    max_longest_edge: int = DEFAULT_MAX_LONGEST_EDGE,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> bytes:
    """Crop the image to ``crop_rect``, resize to ``max_longest_edge``, encode JPEG."""
    cropped = img.crop(crop_rect)
    resized = resize_longest_edge(cropped, max_longest_edge)
    return encode_jpeg(resized, jpeg_quality)


def box_iou(a: dict, b: dict) -> float:
    """Intersection-over-Union of two normalized 0..1 boxes.

    Boxes are ``{"x", "y", "width", "height"}``. Returns 0.0 when either box
    has zero area or the boxes are disjoint, 1.0 when they're identical.
    Used for Stage B diagnostics to detect "Gemini is confident about the
    wrong region" (high box_confidence + low IoU vs reference).
    """
    ax1, ay1 = a["x"], a["y"]
    ax2, ay2 = ax1 + a["width"], ay1 + a["height"]
    bx1, by1 = b["x"], b["y"]
    bx2, by2 = bx1 + b["width"], by1 + b["height"]

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0

    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union
