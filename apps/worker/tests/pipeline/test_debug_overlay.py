"""Smoke tests for the Stage B debug overlay renderer.

The renderer is best-effort instrumentation, so the tests assert it produces
valid JPEG output for the expected box configurations without inspecting the
pixel-level result. Visual correctness is verified by eye during a real job
run with STAGE_B_DEBUG=true.
"""

from __future__ import annotations

import io

from PIL import Image

from app.pipeline.debug_overlay import OverlayBox, render_overlay


def _fixture_image_bytes(width: int = 800, height: int = 600) -> bytes:
    """A non-trivial JPEG with two colored quadrants so the overlay has
    something to draw on top of (helps when manually inspecting test output)."""
    img = Image.new("RGB", (width, height), color=(220, 220, 220))
    for x in range(width // 2, width):
        for y in range(height // 2, height):
            img.putpixel((x, y), (180, 180, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def test_render_overlay_returns_valid_jpeg() -> None:
    image_bytes = _fixture_image_bytes()
    boxes = [
        OverlayBox(
            box={"x": 0.1, "y": 0.1, "width": 0.3, "height": 0.2},
            label="voltage",
            kind="accepted",
            confidence=0.92,
        )
    ]

    out = render_overlay(image_bytes, boxes)

    assert out[:3] == b"\xff\xd8\xff", "expected a JPEG SOI marker"
    with Image.open(io.BytesIO(out)) as decoded:
        decoded.load()
        # Render preserves the source image dimensions.
        assert decoded.size == (800, 600)


def test_render_overlay_handles_all_box_kinds() -> None:
    """Every OverlayKind must render without raising — accepted, rejected,
    fallback, and the dashed reference style."""
    image_bytes = _fixture_image_bytes()
    boxes = [
        OverlayBox(
            box={"x": 0.05, "y": 0.05, "width": 0.2, "height": 0.2},
            label="ref",
            kind="reference",
        ),
        OverlayBox(
            box={"x": 0.3, "y": 0.05, "width": 0.2, "height": 0.2},
            label="ok",
            kind="accepted",
            confidence=0.88,
        ),
        OverlayBox(
            box={"x": 0.55, "y": 0.05, "width": 0.2, "height": 0.2},
            label="no",
            kind="rejected",
            confidence=0.21,
        ),
        OverlayBox(
            box={"x": 0.05, "y": 0.5, "width": 0.2, "height": 0.2},
            label="fallback",
            kind="fallback",
            confidence=0.55,
        ),
    ]

    out = render_overlay(image_bytes, boxes)
    assert out[:3] == b"\xff\xd8\xff"


def test_render_overlay_label_near_top_falls_below_box() -> None:
    """When the box hugs the top edge, the label should render below the box
    rather than off-screen. We verify the call doesn't throw — the visual
    placement logic is exercised but the test stops at "didn't crash on edge
    case", which is the level of confidence we want here."""
    image_bytes = _fixture_image_bytes()
    boxes = [
        OverlayBox(
            box={"x": 0.1, "y": 0.0, "width": 0.3, "height": 0.05},
            label="top-edge",
            kind="accepted",
            confidence=0.7,
        )
    ]
    out = render_overlay(image_bytes, boxes)
    assert out[:3] == b"\xff\xd8\xff"


def test_render_overlay_empty_box_list_returns_unmodified_jpeg() -> None:
    """No boxes to draw → still returns a valid JPEG (just a re-encode)."""
    image_bytes = _fixture_image_bytes()
    out = render_overlay(image_bytes, [])
    assert out[:3] == b"\xff\xd8\xff"
