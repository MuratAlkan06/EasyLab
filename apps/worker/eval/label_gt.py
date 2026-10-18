"""Ground-truth labeling helper: draw GT boxes so they can be checked by eye.

For each image this writes, under ``eval/out/labels/``:

* ``<image>.jpg`` — the whole fixture with every GT box drawn via the
  pipeline's own ``render_overlay`` (green = GT box).
* ``<image>__<field>.jpg`` — a 3x zoom around one box with a pixel ruler, to
  judge tightness against the live display's edges.

Boxes are drawn in UPRIGHT space (EXIF orientation applied first), which is
the space ground truth is defined in. Edit ``ground_truth.json`` and re-run
until every box sits tightly on the live readout.

Usage (from apps/worker)::

    python eval/label_gt.py                       # all images
    python eval/label_gt.py --image target_03.jpg --no-zoom
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

EVAL_DIR = Path(__file__).resolve().parent
# Importable however we're launched: ``app`` (worker dir) and sibling ``gt_io``.
sys.path[:0] = [str(EVAL_DIR.parent), str(EVAL_DIR)]

from gt_io import GTImage, load_ground_truth  # noqa: E402

from app.pipeline.debug_overlay import OverlayBox, render_overlay  # noqa: E402

_ZOOM = 3
_PAD_PX = 24


def upright_jpeg_bytes(path: Path) -> bytes:
    """Re-encode the image with EXIF orientation applied (ground-truth space)."""
    with Image.open(path) as im:
        im.load()
        upright = ImageOps.exif_transpose(im).convert("RGB")
    buf = io.BytesIO()
    upright.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _zoom_with_ruler(canvas: Image.Image, box: dict) -> Image.Image:
    w, h = canvas.size
    x0 = max(0, int(box["x"] * w) - _PAD_PX)
    y0 = max(0, int(box["y"] * h) - _PAD_PX)
    x1 = min(w, int((box["x"] + box["width"]) * w) + _PAD_PX)
    y1 = min(h, int((box["y"] + box["height"]) * h) + _PAD_PX)
    crop = canvas.crop((x0, y0, x1, y1))
    crop = crop.resize((crop.width * _ZOOM, crop.height * _ZOOM), Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(crop)
    for gx in range((x0 // 5 + 1) * 5, x1, 5):
        tick = 14 if gx % 10 == 0 else 6
        draw.line(((gx - x0) * _ZOOM, 0, (gx - x0) * _ZOOM, tick), fill=(0, 255, 255))
        if gx % 20 == 0:
            draw.text(((gx - x0) * _ZOOM + 2, 14), str(gx), fill=(0, 255, 255))
    for gy in range((y0 // 5 + 1) * 5, y1, 5):
        tick = 14 if gy % 10 == 0 else 6
        draw.line((0, (gy - y0) * _ZOOM, tick, (gy - y0) * _ZOOM), fill=(255, 255, 0))
        if gy % 10 == 0:
            draw.text((16, (gy - y0) * _ZOOM - 5), str(gy), fill=(255, 255, 0))
    return crop


def render_image(gt_root: Path, image: GTImage, out_dir: Path, zoom: bool) -> list[Path]:
    upright = upright_jpeg_bytes(gt_root / image.file)
    boxes = [
        OverlayBox(box=b, label=f"GT {name}", kind="accepted")
        for name, b in image.boxes.items()
        if b is not None
    ]
    overlay = render_overlay(upright, boxes)
    stem = Path(image.file).stem
    written = [out_dir / f"{stem}.jpg"]
    written[0].write_bytes(overlay)
    if zoom:
        # Zoom crops come from a label-free render so text never hides an edge.
        for name, b in image.boxes.items():
            if b is None:
                continue
            single = render_overlay(upright, [OverlayBox(box=b, label="", kind="accepted")])
            with Image.open(io.BytesIO(single)) as canvas:
                path = out_dir / f"{stem}__{name}.jpg"
                _zoom_with_ruler(canvas.convert("RGB"), b).save(path, quality=92)
                written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Render GT boxes for visual checking.")
    parser.add_argument("--gt", type=Path, default=EVAL_DIR / "fixtures" / "ground_truth.json")
    parser.add_argument("--out", type=Path, default=EVAL_DIR / "out" / "labels")
    parser.add_argument("--image", action="append", help="limit to these file names")
    parser.add_argument("--no-zoom", action="store_true", help="skip per-field zoom crops")
    args = parser.parse_args()

    gt = load_ground_truth(args.gt)  # validates format + derived-box consistency
    args.out.mkdir(parents=True, exist_ok=True)
    for image in [gt.reference, *gt.targets]:
        if args.image and image.file not in args.image:
            continue
        for path in render_image(gt.root, image, args.out, zoom=not args.no_zoom):
            print(path)


if __name__ == "__main__":
    main()
