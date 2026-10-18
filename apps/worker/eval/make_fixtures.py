"""Build the committed eval fixtures from private full-frame originals.

The originals live OUTSIDE version control (they show people); this script
reads a private spec, writes privacy-safe fixtures, and verifies them. Nothing
here hard-codes a source path, date, or location — all of that is in the spec,
which belongs in the gitignored ``eval/fixtures_private/`` directory.

Usage (from apps/worker)::

    python eval/make_fixtures.py --spec eval/fixtures_private/fixture_spec.json

Spec format (JSON)::

    {
      "crop": [left, top, right, bottom],     # source px, shared by every frame
      "fill_rgb": [128, 128, 128],            # flat redaction colour
      "max_edge": 1600,                       # longest-edge cap for outputs
      "jpeg_quality": 90,
      "frames": [
        {
          "source": "/abs/path/to/original.jpg",
          "output": "target_01.jpg",
          # Polygons in SOURCE px. Pixels start redacted; "keep" polygons are
          # revealed, then "redact" polygons are re-covered (hands etc.).
          "keep": [[[x, y], ...], ...],
          "redact": [[[x, y], ...], ...]
        }
      ],
      "derived": [
        # Upright pixels of target_02 rotated 90 deg CCW + EXIF Orientation=6,
        # so an EXIF-aware reader shows exactly target_02.
        {"output": "target_11.jpg", "from": "target_02.jpg", "op": "exif6"},
        # Deterministic photometric attack: gamma/colour cast/contrast plus
        # Gaussian specular glare blobs (normalized centre + radii).
        {"output": "target_12.jpg", "from": "target_01.jpg", "op": "lighting",
         "gamma": 1.6, "gain_rgb": [1.0, 0.88, 0.7], "contrast": 0.75,
         "glare": [{"cx": 0.1, "cy": 0.4, "rx": 0.05, "ry": 0.04, "strength": 0.9}]}
      ]
    }

Every output is re-encoded from raw pixels (no EXIF, ICC, XMP, or comments
survive), except the exif6 fixture whose EXIF block holds the Orientation tag
only. ``verify_fixture`` asserts this after writing.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

_EXIF_ORIENTATION = 0x0112
# JPEG markers that may carry metadata: APP1..APP15 and COM. APP0 (JFIF) is the
# baseline header Pillow always writes and carries only pixel density.
_METADATA_MARKERS = {*range(0xE1, 0xF0), 0xFE}


def _polys(polys: list) -> list[list[tuple[float, float]]]:
    return [[(float(x), float(y)) for x, y in poly] for poly in polys]


def build_frame(frame: dict, crop: list[int], fill_rgb: list[int], max_edge: int) -> Image.Image:
    """Crop + redact one source frame; returns an upright, metadata-free RGB image."""
    with Image.open(frame["source"]) as src:
        src.load()
        img = ImageOps.exif_transpose(src).convert("RGB")

    # 255 = redact, 0 = keep. Start fully redacted so anything not explicitly
    # kept (background, bystanders) can never leak through a gap in the spec.
    mask = Image.new("L", img.size, 255)
    draw = ImageDraw.Draw(mask)
    for poly in _polys(frame.get("keep", [])):
        draw.polygon(poly, fill=0)
    for poly in _polys(frame.get("redact", [])):
        draw.polygon(poly, fill=255)

    fill = Image.new("RGB", img.size, tuple(fill_rgb))
    out = Image.composite(fill, img, mask).crop(tuple(crop))
    if max(out.size) > max_edge:
        out.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    # Rebuild from raw bytes so no ``info`` (icc/exif/comment) can ride along.
    return Image.frombytes("RGB", out.size, out.tobytes())


def apply_lighting(img: Image.Image, op: dict) -> Image.Image:
    """Deterministic adversarial lighting: tone/colour shift + specular glare."""
    arr = np.asarray(img, dtype=np.float32) / 255.0
    arr = arr ** float(op.get("gamma", 1.0))
    arr = arr * np.asarray(op.get("gain_rgb", [1.0, 1.0, 1.0]), dtype=np.float32)
    mean = arr.mean(axis=(0, 1), keepdims=True)
    arr = (arr - mean) * float(op.get("contrast", 1.0)) + mean

    h, w = arr.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    for g in op.get("glare", []):
        cx, cy = g["cx"] * w, g["cy"] * h
        rx, ry = max(1.0, g["rx"] * w), max(1.0, g["ry"] * h)
        blob = np.exp(-(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2))
        alpha = (float(g.get("strength", 0.8)) * blob)[..., None]
        arr = arr * (1.0 - alpha) + alpha  # blend toward white
    arr = np.clip(arr * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, mode="RGB")


def _save_jpeg(img: Image.Image, path: Path, quality: int, exif: Image.Exif | None = None) -> None:
    kwargs: dict = {"format": "JPEG", "quality": quality, "optimize": True}
    if exif is not None:
        kwargs["exif"] = exif.tobytes()
    img.save(path, **kwargs)


def _jpeg_metadata_markers(data: bytes) -> list[int]:
    """Return metadata-bearing marker codes found before the first SOS."""
    found = []
    i = 2  # skip SOI
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:  # start of scan — header is over
            break
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in _METADATA_MARKERS:
            found.append(marker)
        i += 2 + seg_len
    return found


def verify_fixture(path: Path, *, expect_orientation: int | None, max_edge: int) -> None:
    """Assert the written file carries no metadata beyond what's intended."""
    data = path.read_bytes()
    markers = _jpeg_metadata_markers(data)
    allowed = {"jfif", "jfif_version", "jfif_unit", "jfif_density", "exif"}
    if expect_orientation is not None:
        # Pillow synthesizes dpi=(72, 72) for any EXIF block lacking resolution
        # tags; it is not stored in the file (the marker/EXIF checks prove that).
        allowed.add("dpi")
    with Image.open(io.BytesIO(data)) as im:
        exif = dict(im.getexif())
        size = im.size
        info_keys = set(im.info) - allowed
    assert max(size) <= max_edge, f"{path.name}: {size} exceeds {max_edge}px"
    assert not info_keys, f"{path.name}: unexpected metadata {sorted(info_keys)}"
    if expect_orientation is None:
        assert not exif, f"{path.name}: EXIF present {exif}"
        assert not markers, f"{path.name}: metadata markers {[hex(m) for m in markers]}"
    else:
        assert exif == {_EXIF_ORIENTATION: expect_orientation}, f"{path.name}: EXIF {exif}"
        assert markers == [0xE1], f"{path.name}: markers {[hex(m) for m in markers]}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "fixtures")
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text())
    crop, fill = spec["crop"], spec.get("fill_rgb", [128, 128, 128])
    max_edge = int(spec.get("max_edge", 1600))
    quality = int(spec.get("jpeg_quality", 90))
    args.out.mkdir(parents=True, exist_ok=True)

    built: dict[str, Image.Image] = {}
    for frame in spec["frames"]:
        img = build_frame(frame, crop, fill, max_edge)
        built[frame["output"]] = img
        path = args.out / frame["output"]
        _save_jpeg(img, path, quality)
        verify_fixture(path, expect_orientation=None, max_edge=max_edge)
        print(f"wrote {path.name} {img.size}")

    for d in spec.get("derived", []):
        upright = built[d["from"]]  # in-memory, so derived files are single-generation
        path = args.out / d["output"]
        if d["op"] == "exif6":
            exif = Image.Exif()
            exif[_EXIF_ORIENTATION] = 6
            # Orientation 6 = "rotate 90 CW to display", so store it 90 CCW.
            _save_jpeg(upright.transpose(Image.Transpose.ROTATE_90), path, quality, exif)
            verify_fixture(path, expect_orientation=6, max_edge=max_edge)
            with Image.open(path) as im:
                shown = ImageOps.exif_transpose(im)
                assert shown.size == upright.size, f"{path.name}: upright size {shown.size}"
                err = np.abs(np.asarray(shown, np.int16) - np.asarray(upright, np.int16)).mean()
            assert err < 3.0, f"{path.name}: EXIF-transposed pixels differ (mean abs {err:.2f})"
            print(f"wrote {path.name} raw={upright.size[::-1]} orientation=6 (mean abs {err:.2f})")
        elif d["op"] == "lighting":
            _save_jpeg(apply_lighting(upright, d), path, quality)
            verify_fixture(path, expect_orientation=None, max_edge=max_edge)
            print(f"wrote {path.name} lighting from {d['from']}")
        else:
            raise SystemExit(f"unknown derived op: {d['op']}")

    total = sum(p.stat().st_size for p in args.out.glob("*.jpg"))
    print(f"total fixture JPEG bytes: {total} ({total / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
