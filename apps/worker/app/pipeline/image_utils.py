"""Image preprocessing utilities used across pipeline stages.

All operations work in-memory on Pillow Image objects. The full Stage A entry
point (`preprocess_for_stage_a`) takes raw bytes and returns JPEG bytes ready
to send to Gemini.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

# Stage A targets Gemini's optimal input edge length and a high-quality JPEG.
STAGE_A_LONGEST_EDGE_PX = 1568
STAGE_A_JPEG_QUALITY = 90

# Stage B uses the same edge length but a slightly lower quality. Detection
# needs less fine detail than the reference-image template generation pass.
STAGE_B_LONGEST_EDGE_PX = 1568
STAGE_B_JPEG_QUALITY = 88


def transpose_exif(img: Image.Image) -> Image.Image:
    """Apply EXIF orientation tag so the image is upright.

    Pillow's ImageOps.exif_transpose returns a new image when rotation is
    needed, otherwise the original.
    """
    return ImageOps.exif_transpose(img)


def resize_longest_edge(img: Image.Image, max_px: int) -> Image.Image:
    """Resize so the longest edge equals `max_px`, preserving aspect ratio.

    Returns the original image unchanged if it is already at or below the
    target size on its longest edge.
    """
    width, height = img.size
    longest = max(width, height)
    if longest <= max_px:
        return img
    scale = max_px / longest
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return img.resize(new_size, Image.LANCZOS)


def encode_jpeg(img: Image.Image, quality: int) -> bytes:
    """Encode an image to JPEG bytes in memory.

    Converts non-RGB modes (RGBA, P, etc.) to RGB before encoding because
    JPEG does not support alpha or palette modes.
    """
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def preprocess_for_stage_a(img_bytes: bytes) -> bytes:
    """Full Stage A preprocessing: EXIF transpose -> resize 1568px -> JPEG q=90."""
    with Image.open(io.BytesIO(img_bytes)) as img:
        img.load()
        img = transpose_exif(img)
        img = resize_longest_edge(img, STAGE_A_LONGEST_EDGE_PX)
        return encode_jpeg(img, STAGE_A_JPEG_QUALITY)


def preprocess_for_stage_b(img_bytes: bytes) -> bytes:
    """Full Stage B preprocessing: EXIF transpose -> resize 1568px -> JPEG q=88."""
    with Image.open(io.BytesIO(img_bytes)) as img:
        img.load()
        img = transpose_exif(img)
        img = resize_longest_edge(img, STAGE_B_LONGEST_EDGE_PX)
        return encode_jpeg(img, STAGE_B_JPEG_QUALITY)
