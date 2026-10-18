"""Load + validate an eval ground-truth file (see eval/README.md for the format).

Pure stdlib; shared by run_eval.py and label_gt.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CLASSES = ("aligned", "shifted", "exif6", "lighting")
# Ops that keep every pixel where it was in upright space, so the derived
# image's ground truth must equal its source's.
_PIXEL_PRESERVING_DERIVATIONS = ("exif6", "lighting")


@dataclass(frozen=True)
class Field:
    name: str
    semantic_description: str


@dataclass(frozen=True)
class GTImage:
    file: str
    cls: str  # "reference" or one of CLASSES
    boxes: dict[str, dict[str, float] | None]  # field -> {"x","y","width","height"} | None
    derived_from: str | None = None


@dataclass(frozen=True)
class GroundTruth:
    root: Path
    fields: list[Field]
    reference: GTImage
    targets: list[GTImage]

    def path(self, image: GTImage) -> Path:
        return self.root / image.file


def box_from_list(xywh: list[float] | None) -> dict[str, float] | None:
    """``[x, y, w, h]`` (normalized) -> the pipeline's ``{"x","y","width","height"}`` dict."""
    if xywh is None:
        return None
    x, y, w, h = (float(v) for v in xywh)
    return {"x": x, "y": y, "width": w, "height": h}


def _check_box(where: str, xywh: list[float]) -> None:
    if len(xywh) != 4:
        raise ValueError(f"{where}: box must be [x, y, w, h], got {xywh}")
    x, y, w, h = xywh
    if not (0 <= x <= 1 and 0 <= y <= 1 and w > 0 and h > 0):
        raise ValueError(f"{where}: box out of range {xywh}")
    if x + w > 1 + 1e-6 or y + h > 1 + 1e-6:
        raise ValueError(f"{where}: box extends past the image {xywh}")


def load_ground_truth(gt_path: Path) -> GroundTruth:
    raw = json.loads(gt_path.read_text())
    root = gt_path.parent
    fields = [Field(f["name"], f["semantic_description"]) for f in raw["fields"]]
    names = {f.name for f in fields}

    images: list[GTImage] = []
    for entry in raw["images"]:
        file, cls = entry["file"], entry["class"]
        if cls != "reference" and cls not in CLASSES:
            raise ValueError(f"{file}: unknown class {cls!r} (expected one of {CLASSES})")
        if not (root / file).is_file():
            raise ValueError(f"{file}: image missing from {root}")
        boxes_raw = entry["boxes"]
        if set(boxes_raw) != names:
            raise ValueError(f"{file}: boxes must list exactly the fields {sorted(names)}")
        for name, xywh in boxes_raw.items():
            if xywh is not None:
                _check_box(f"{file}/{name}", xywh)
        images.append(
            GTImage(
                file=file,
                cls=cls,
                boxes={n: box_from_list(v) for n, v in boxes_raw.items()},
                derived_from=entry.get("derived_from"),
            )
        )

    refs = [im for im in images if im.cls == "reference"]
    if len(refs) != 1 or refs[0].file != raw["reference"]:
        raise ValueError("ground truth needs exactly one image with class 'reference'")
    by_file = {im.file: im for im in images}
    for im in images:
        if im.derived_from and im.cls in _PIXEL_PRESERVING_DERIVATIONS:
            src = by_file.get(im.derived_from)
            if src is None:
                raise ValueError(f"{im.file}: derived_from {im.derived_from!r} not in file")
            if src.boxes != im.boxes:
                raise ValueError(f"{im.file}: boxes must equal {src.file} (pixel-preserving op)")
    return GroundTruth(
        root=root,
        fields=fields,
        reference=refs[0],
        targets=[im for im in images if im.cls != "reference"],
    )
