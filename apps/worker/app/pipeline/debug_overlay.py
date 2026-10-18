"""Render Stage B detection results onto a copy of the source image.

The Stage B debug flag (``STAGE_B_DEBUG=true``) uses this to emit annotated
JPEGs to Supabase Storage so we can eyeball what Gemini is actually boxing.
It's the fastest path from "the cells table looks wrong" to "yep, the model
boxed the AMP label again" without rebuilding the review UI.

Color convention (matches the eventual review UI):

* **green** — accepted Gemini detection
* **red**   — rejected Gemini detection (low confidence or invalid geometry)
* **blue**  — reference-box fallback
* **yellow dashed** — the user's reference annotation (always drawn for context)

This is a pure-PIL helper; no I/O. The caller decides where the bytes go.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

OverlayKind = Literal["accepted", "rejected", "fallback", "reference"]

_COLORS: dict[OverlayKind, tuple[int, int, int]] = {
    "accepted": (34, 197, 94),  # emerald-500
    "rejected": (239, 68, 68),  # red-500
    "fallback": (59, 130, 246),  # blue-500
    "reference": (234, 179, 8),  # amber-500 (drawn dashed)
}


@dataclass(frozen=True)
class OverlayBox:
    """One normalized 0..1 box to render, with a label and a kind.

    ``label`` is drawn above the box in the matching color. ``confidence`` is
    appended in parens when present.
    """

    box: dict  # {"x", "y", "width", "height"} in 0..1
    label: str
    kind: OverlayKind
    confidence: float | None = None


def render_overlay(image_bytes: bytes, boxes: Iterable[OverlayBox]) -> bytes:
    """Return a JPEG with the boxes drawn on top of the original image.

    The original is opened, copied (so the input bytes aren't mutated), drawn
    on, and re-encoded as a moderate-quality JPEG suitable for browser viewing.
    """
    with Image.open(io.BytesIO(image_bytes)) as src:
        src.load()
        canvas = src.convert("RGB")

    draw = ImageDraw.Draw(canvas)
    width, height = canvas.size

    try:
        font = ImageFont.load_default()
    except Exception:  # noqa: BLE001
        font = None

    for entry in boxes:
        color = _COLORS[entry.kind]
        x1 = entry.box["x"] * width
        y1 = entry.box["y"] * height
        x2 = x1 + entry.box["width"] * width
        y2 = y1 + entry.box["height"] * height

        if entry.kind == "reference":
            _draw_dashed_rect(draw, (x1, y1, x2, y2), color, dash=8, width=2)
        else:
            draw.rectangle((x1, y1, x2, y2), outline=color, width=3)

        text = entry.label
        if entry.confidence is not None:
            text = f"{text} ({entry.confidence:.2f})"

        # Label background — small filled box so the text reads on busy images.
        # Place above the box; fall below if there's no room at the top.
        text_anchor = (x1, max(0.0, y1 - 16))
        if y1 < 18:
            text_anchor = (x1, min(height - 16, y2 + 2))
        if font is not None:
            bbox = draw.textbbox(text_anchor, text, font=font)
            draw.rectangle(bbox, fill=color)
            draw.text(text_anchor, text, fill=(255, 255, 255), font=font)
        else:
            draw.text(text_anchor, text, fill=color)

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


def _draw_dashed_rect(
    draw: ImageDraw.ImageDraw,
    rect: tuple[float, float, float, float],
    color: tuple[int, int, int],
    *,
    dash: int = 8,
    width: int = 2,
) -> None:
    """Render a dashed rectangle. PIL has no native dash for shapes, so we
    fake it with short line segments. ``dash`` is the on/off length in pixels."""
    x1, y1, x2, y2 = rect

    def _dashed_line(start: tuple[float, float], end: tuple[float, float]) -> None:
        sx, sy = start
        ex, ey = end
        dx, dy = ex - sx, ey - sy
        length = (dx * dx + dy * dy) ** 0.5
        if length <= 0:
            return
        ux, uy = dx / length, dy / length
        pos = 0.0
        on = True
        while pos < length:
            seg = min(dash, length - pos)
            if on:
                draw.line(
                    (sx + ux * pos, sy + uy * pos, sx + ux * (pos + seg), sy + uy * (pos + seg)),
                    fill=color,
                    width=width,
                )
            pos += seg
            on = not on

    _dashed_line((x1, y1), (x2, y1))
    _dashed_line((x2, y1), (x2, y2))
    _dashed_line((x2, y2), (x1, y2))
    _dashed_line((x1, y2), (x1, y1))
