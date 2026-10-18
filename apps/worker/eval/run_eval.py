"""Offline Stage B detection eval harness (Phase 7 / ADR-007 Slice 1).

Scores a detector backend against hand-labeled ground truth and prints mean
IoU + hit-rate@threshold per field, per fixture class, aggregate, and the
worst case per image. Overlay JPEGs (GT vs prediction) go to ``eval/out/``.

Backends:
  reference  predicted box = the reference image's GT box for that field
             (simulates Stage B's reference_box fallback). Offline, no network.
  align      production Stage B: app.pipeline.align homography + NCC transfer
             through Stage B's acceptance ladder. Offline.
  gemini     the pre-Phase-7 Gemini detector, frozen in ``legacy_gemini.py``
             (key-gated, network, costs money). Kept for before/after numbers.

Scoring space: ground truth is UPRIGHT (EXIF applied). A predicted box is what
Stage B hands to Stage C, and Stage C crops it from whatever canvas
``stage_c._open_image`` returns. The harness asks Stage C for that canvas and
maps the prediction into upright space before scoring, so any EXIF handling
regression shows up as low IoU on the exif6 fixture.

The harness imports pipeline modules; nothing in ``app`` imports the harness.
Usage (from apps/worker)::

    python eval/run_eval.py --backend reference
    python eval/run_eval.py --backend align
    python eval/run_eval.py --backend gemini --iou-threshold 0.5
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

# A3: app.settings builds Settings() at import time and requires these. The
# placeholders also guarantee the harness can never reach Supabase/Postgres.
# GEMINI_API_KEY is deliberately NOT defaulted (the gemini backend gates on it).
os.environ.setdefault("SUPABASE_URL", "https://placeholder.local")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "placeholder")
os.environ.setdefault("DATABASE_URL", "postgresql://placeholder/placeholder")
os.environ.setdefault("INTERNAL_SHARED_SECRET", "placeholder")

EVAL_DIR = Path(__file__).resolve().parent
# Importable however we're launched: ``app`` (worker dir) and sibling ``gt_io``.
sys.path[:0] = [str(EVAL_DIR.parent), str(EVAL_DIR)]

from gt_io import CLASSES, GroundTruth, GTImage, load_ground_truth  # noqa: E402
from PIL import Image, ImageChops, ImageOps  # noqa: E402

from app.pipeline.crop_utils import box_iou, is_box_valid  # noqa: E402
from app.pipeline.debug_overlay import OverlayBox, render_overlay  # noqa: E402

Box = dict[str, float]

# Normalized point maps raw -> upright for each EXIF orientation, matching the
# transposes ImageOps.exif_transpose applies.
_RAW_TO_UPRIGHT: dict[int, Callable[[float, float], tuple[float, float]]] = {
    2: lambda x, y: (1 - x, y),
    3: lambda x, y: (1 - x, 1 - y),
    4: lambda x, y: (x, 1 - y),
    5: lambda x, y: (y, x),
    6: lambda x, y: (1 - y, x),
    7: lambda x, y: (1 - y, 1 - x),
    8: lambda x, y: (y, 1 - x),
}


# --------------------------------------------------------------------------- #
# Stage C crop-canvas space
# --------------------------------------------------------------------------- #


def _stage_c_canvas(image_bytes: bytes) -> Image.Image:
    """The image Stage C crops from — delegated, so scoring always uses Stage C's real canvas."""
    from app.pipeline.stage_c import _open_image

    return _open_image(image_bytes)


@dataclass(frozen=True)
class CanvasInfo:
    orientation: int  # EXIF orientation tag (1 when absent)
    space: str  # "upright" or "raw"


def _same_pixels(a: Image.Image, b: Image.Image) -> bool:
    return a.size == b.size and ImageChops.difference(a, b).getbbox() is None


def canvas_info(image_bytes: bytes) -> CanvasInfo:
    with Image.open(io.BytesIO(image_bytes)) as im:
        im.load()
        orientation = int(im.getexif().get(0x0112, 1) or 1)
        raw = im.convert("RGB")
        upright = ImageOps.exif_transpose(im).convert("RGB")
    canvas = _stage_c_canvas(image_bytes).convert("RGB")
    if _same_pixels(canvas, upright):
        return CanvasInfo(orientation, "upright")
    if _same_pixels(canvas, raw):
        return CanvasInfo(orientation, "raw")
    raise RuntimeError("Stage C canvas matches neither the raw nor the upright decode")


def to_upright(box: Box, info: CanvasInfo) -> Box:
    """Map a box Stage C would apply to its canvas into upright normalized space."""
    if info.space == "upright" or info.orientation not in _RAW_TO_UPRIGHT:
        return box
    fn = _RAW_TO_UPRIGHT[info.orientation]
    x0, y0 = box["x"], box["y"]
    x1, y1 = x0 + box["width"], y0 + box["height"]
    pts = [fn(x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys)}


# --------------------------------------------------------------------------- #
# Backends: each returns {field_name: Prediction} for one target image
# --------------------------------------------------------------------------- #


@dataclass
class Prediction:
    box: Box | None  # as handed to Stage C (normalized vs. the original image)
    source: str  # "reference" | "gemini" | an align method | "fallback" | "failed"
    confidence: float | None = None
    detector_box: Box | None = None  # raw detector output, even if rejected


@dataclass
class ImageResult:
    predictions: dict[str, Prediction]
    latency_s: float
    error: str | None = None


def template_fields(gt: GroundTruth) -> list[dict]:
    """Fields a user could have annotated: those visible in the reference."""
    by_name = {f.name: f for f in gt.fields}
    return [
        {
            "field_name": name,
            "semantic_description": by_name[name].semantic_description,
            "reference_box": box,
        }
        for name, box in gt.reference.boxes.items()
        if box is not None
    ]


def run_reference(gt: GroundTruth, fields: list[dict]) -> dict[str, ImageResult]:
    out = {}
    for target in gt.targets:
        t0 = time.perf_counter()
        preds = {f["field_name"]: Prediction(f["reference_box"], "reference") for f in fields}
        out[target.file] = ImageResult(preds, time.perf_counter() - t0)
    return out


def run_align(gt: GroundTruth, fields: list[dict]) -> dict[str, ImageResult]:
    """Production Stage B per target: reference features prepared once, then
    ``align.align_target`` and the same acceptance ladder as
    ``stage_b._process_image``. Same module, same constants: no eval-only knobs.
    Latency is wall time of ``align_target`` (decode + features + H + NCC)."""
    from app.pipeline import align, stage_b

    t0 = time.perf_counter()
    reference = align.prepare_reference(
        gt.path(gt.reference).read_bytes(),
        {f["field_name"]: f["reference_box"] for f in fields},
    )
    print(
        f"align backend: reference prepared in {time.perf_counter() - t0:.3f}s "
        f"({len(reference.points)} masked features)"
    )

    out = {}
    for target in gt.targets:
        raw = gt.path(target).read_bytes()
        width, height = stage_b._open_dimensions(raw)
        t0 = time.perf_counter()
        result = align.align_target(reference, raw)
        latency = time.perf_counter() - t0

        preds = {}
        for f in fields:
            name, ref_box = f["field_name"], f["reference_box"]
            det = result.fields.get(name)
            raw_box = det.box if det is not None and det.method != align.METHOD_FALLBACK else None
            conf = det.box_confidence if det is not None else 0.0
            if (
                raw_box is not None
                and conf >= stage_b._MIN_BOX_CONFIDENCE
                and is_box_valid(raw_box, width, height)
            ):
                preds[name] = Prediction(raw_box, det.method, conf, raw_box)
            elif ref_box and is_box_valid(ref_box, width, height):
                preds[name] = Prediction(
                    ref_box, "fallback", stage_b._REFERENCE_BOX_CONFIDENCE, raw_box
                )
            else:
                preds[name] = Prediction(None, "failed", conf, raw_box)
        print(
            f"  {target.file}: {latency * 1000:.0f} ms  H={result.h_status} "
            f"matches={result.matches} inliers={result.inliers}"
        )
        out[target.file] = ImageResult(preds, latency)
    return out


def gemini_unavailable_reason() -> str | None:
    try:
        import google.genai  # noqa: F401
    except ImportError as exc:
        return f"google-genai is not importable in this environment ({exc})"
    from app.settings import settings

    if not settings.gemini_api_key:
        return (
            "GEMINI_API_KEY is not set (export it, or run from apps/worker so "
            "pydantic-settings reads apps/worker/.env)"
        )
    return None


def run_gemini(gt: GroundTruth, fields: list[dict]) -> dict[str, ImageResult]:
    """Reproduce the pre-Phase-7 Gemini Stage B per target from the frozen
    ``legacy_gemini`` snapshot: same preprocessing, prompt, model, retry
    (2 attempts, 2 s apart), and acceptance ladder."""
    import legacy_gemini
    from tenacity import AsyncRetrying, stop_after_attempt, wait_fixed

    from app.pipeline import stage_b
    from app.settings import settings

    async def call_with_retry(image_bytes: bytes):
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(2), wait=wait_fixed(2.0), reraise=True
        ):
            with attempt:
                return await legacy_gemini.call_stage_b(client, model, image_bytes, fields)

    client = legacy_gemini.make_client(settings.gemini_api_key)
    model = legacy_gemini.MODEL
    sem = asyncio.Semaphore(settings.image_concurrency)
    print(f"gemini backend: model={model} targets={len(gt.targets)} fields={len(fields)}")

    def ladder(parsed, width: int, height: int) -> dict[str, Prediction]:
        by_name = {df.field_name: df for df in parsed.fields} if parsed else {}
        preds = {}
        for f in fields:
            df, ref_box = by_name.get(f["field_name"]), f["reference_box"]
            raw_box = (
                legacy_gemini.convert_box_to_normalized(df.box)
                if df is not None and df.box is not None
                else None
            )
            conf = float(df.box_confidence or 0.0) if df is not None else 0.0
            accepted = (
                df is not None
                and df.found
                and raw_box is not None
                and conf >= stage_b._MIN_BOX_CONFIDENCE
                and is_box_valid(raw_box, width, height)
            )
            if accepted:
                preds[f["field_name"]] = Prediction(raw_box, "gemini", conf, raw_box)
            elif ref_box and is_box_valid(ref_box, width, height):
                preds[f["field_name"]] = Prediction(
                    ref_box, "fallback", stage_b._REFERENCE_BOX_CONFIDENCE, raw_box
                )
            else:
                preds[f["field_name"]] = Prediction(None, "failed", conf, raw_box)
        return preds

    async def one(target: GTImage) -> tuple[str, ImageResult]:
        raw = gt.path(target).read_bytes()
        width, height = stage_b._open_dimensions(raw)
        image_bytes = legacy_gemini.preprocess(raw)
        async with sem:
            t0 = time.perf_counter()
            try:
                parsed = await call_with_retry(image_bytes)
                error = None
            except Exception as exc:  # noqa: BLE001 — production falls back on any error
                parsed, error = None, f"{type(exc).__name__}: {exc}"[:300]
            latency = time.perf_counter() - t0
        print(f"  {target.file}: {latency:.1f}s" + (f" ERROR {error}" if error else ""))
        return target.file, ImageResult(ladder(parsed, width, height), latency, error)

    async def run_all() -> dict[str, ImageResult]:
        return dict(await asyncio.gather(*(one(t) for t in gt.targets)))

    return asyncio.run(run_all())


BACKENDS = {"reference": run_reference, "align": run_align, "gemini": run_gemini}


# --------------------------------------------------------------------------- #
# Scoring + report
# --------------------------------------------------------------------------- #


@dataclass
class Pair:
    image: str
    cls: str
    field: str
    iou: float
    hit: bool
    source: str
    gt_box: Box
    pred_box: Box | None
    effective_box: Box | None  # pred mapped to upright via the Stage C canvas
    detector_iou: float | None  # raw detector box (pre-ladder), same mapping


def _stats(pairs: list[Pair]) -> dict:
    if not pairs:
        return {"n": 0, "mean_iou": None, "hit_rate": None, "min_iou": None}
    ious = [p.iou for p in pairs]
    return {
        "n": len(pairs),
        "mean_iou": round(sum(ious) / len(ious), 4),
        "hit_rate": round(sum(p.hit for p in pairs) / len(pairs), 4),
        "min_iou": round(min(ious), 4),
    }


def score(
    gt: GroundTruth, fields: list[dict], results: dict[str, ImageResult], thr: float
) -> tuple[list[Pair], dict[str, CanvasInfo], list[str]]:
    pairs, canvases, notes = [], {}, []
    in_template = {f["field_name"] for f in fields}
    for name in sorted({f.name for f in gt.fields} - in_template):
        notes.append(f"{name}: absent in reference -> not in template, not scored")
    for target in gt.targets:
        info = canvas_info(gt.path(target).read_bytes())
        canvases[target.file] = info
        preds = results[target.file].predictions
        for name, gt_box in target.boxes.items():
            if name not in in_template:
                continue
            pred = preds[name]
            if gt_box is None:
                if pred.box is not None:
                    notes.append(f"{target.file}/{name}: GT absent but backend predicted a box")
                continue
            eff = to_upright(pred.box, info) if pred.box is not None else None
            iou = box_iou(eff, gt_box) if eff is not None else 0.0
            det = (
                box_iou(to_upright(pred.detector_box, info), gt_box)
                if pred.detector_box is not None
                else None
            )
            pairs.append(
                Pair(
                    target.file,
                    target.cls,
                    name,
                    round(iou, 4),
                    iou >= thr,
                    pred.source,
                    gt_box,
                    pred.box,
                    eff,
                    None if det is None else round(det, 4),
                )
            )
    return pairs, canvases, notes


def _cell(s: dict) -> str:
    if not s["n"]:
        return "-"
    return f"{s['mean_iou']:.3f} / {s['hit_rate']:.2f} ({s['n']})"


def report(
    backend: str,
    gt: GroundTruth,
    pairs: list[Pair],
    canvases: dict[str, CanvasInfo],
    notes: list[str],
    results: dict[str, ImageResult],
    thr: float,
) -> dict:
    field_names = [f.name for f in gt.fields]
    classes = [c for c in CLASSES if any(t.cls == c for t in gt.targets)]
    raw_canvas = sorted(f for f, c in canvases.items() if c.space == "raw" and c.orientation != 1)

    def sel(field: str | None = None, cls: str | None = None) -> list[Pair]:
        return [
            p
            for p in pairs
            if (field is None or p.field == field) and (cls is None or p.cls == cls)
        ]

    lines = [
        f"EasyLab Stage B eval  backend={backend}  hit = IoU >= {thr:.2f}",
        f"fixtures: {gt.root}  ({1 + len(gt.targets)} images: 1 reference + "
        f"{len(gt.targets)} targets; {len(pairs)} scored field instances)",
        "scoring space: upright GT; predictions mapped through Stage C's crop canvas"
        + (f" (raw/EXIF-unapplied for {', '.join(raw_canvas)})" if raw_canvas else ""),
        "",
        "Per field x class: mean IoU / hit-rate (n)",
    ]
    w = 21
    lines.append(f"{'field':<{w}}" + "".join(f"{c:<20}" for c in [*classes, "ALL"]))
    for fname in field_names:
        cells = [_cell(_stats(sel(fname, c))) for c in classes] + [_cell(_stats(sel(fname)))]
        lines.append(f"{fname:<{w}}" + "".join(f"{x:<20}" for x in cells))
    cells = [_cell(_stats(sel(cls=c))) for c in classes] + [_cell(_stats(pairs))]
    lines.append(f"{'ALL FIELDS':<{w}}" + "".join(f"{x:<20}" for x in cells))

    lines += ["", "Per image (worst field first)"]
    lines.append(
        f"{'image':<15}{'class':<10}{'mean IoU':>9}{'hits':>7}  {'worst field':<21}{'IoU':>6}"
        + ("  sources" if backend != "reference" else "")
    )
    per_image = []
    for target in gt.targets:
        ps = [p for p in pairs if p.image == target.file]
        if not ps:
            continue
        worst = min(ps, key=lambda p: p.iou)
        srcs: dict[str, int] = {}
        for p in ps:
            srcs[p.source] = srcs.get(p.source, 0) + 1
        per_image.append(
            {
                "image": target.file,
                "class": target.cls,
                "mean_iou": round(sum(p.iou for p in ps) / len(ps), 4),
                "hits": sum(p.hit for p in ps),
                "n": len(ps),
                "worst_field": worst.field,
                "worst_iou": worst.iou,
                "sources": srcs,
                "error": results[target.file].error,
            }
        )
    for r in sorted(per_image, key=lambda r: r["worst_iou"]):
        src = "  " + ",".join(f"{k}={v}" for k, v in sorted(r["sources"].items()))
        lines.append(
            f"{r['image']:<15}{r['class']:<10}{r['mean_iou']:>9.3f}{r['hits']:>4}/{r['n']:<2}  "
            f"{r['worst_field']:<21}{r['worst_iou']:>6.3f}"
            + (src if backend != "reference" else "")
        )
    if pairs:
        worst = min(pairs, key=lambda p: p.iou)
        lines.append(f"worst case: {worst.image} / {worst.field}  IoU={worst.iou:.3f}")

    if backend != "reference":
        lines += ["", "Detector diagnostics (raw boxes before the acceptance ladder)"]
        lines.append(f"{'field':<{w}}{'accepted':>9}{'fallback':>9}{'failed':>7}{'raw IoU':>9}")
        for fname in field_names:
            ps = sel(fname)
            if not ps:
                continue
            dets = [p.detector_iou for p in ps if p.detector_iou is not None]
            raw = f"{sum(dets) / len(dets):.3f}" if dets else "-"
            fallback = sum(p.source == "fallback" for p in ps)
            failed = sum(p.source == "failed" for p in ps)
            accepted = len(ps) - fallback - failed
            lines.append(f"{fname:<{w}}{accepted:>9}{fallback:>9}{failed:>7}{raw:>9}")
        lat = [r.latency_s for r in results.values()]
        errs = sum(r.error is not None for r in results.values())
        lines.append(
            f"latency/image: mean {sum(lat) / len(lat):.3f}s  max {max(lat):.3f}s  "
            f"detector errors: {errs}/{len(lat)}"
        )
    if notes:
        lines += ["", "Notes:"] + [f"  - {n}" for n in notes]
    print("\n".join(lines))

    return {
        "backend": backend,
        "fixtures": str(gt.root),
        "iou_threshold": thr,
        "summary": {
            "overall": _stats(pairs),
            "by_class": {c: _stats(sel(cls=c)) for c in classes},
            "by_field": {f: _stats(sel(f)) for f in field_names},
            "by_field_class": {f: {c: _stats(sel(f, c)) for c in classes} for f in field_names},
        },
        "per_image": per_image,
        "canvas": {k: asdict(v) for k, v in canvases.items()},
        "pairs": [asdict(p) for p in pairs],
        "notes": notes,
    }


def write_overlays(gt: GroundTruth, pairs: list[Pair], out_dir: Path) -> None:
    """GT = green, prediction = blue. Drawn by the pipeline's render_overlay on
    the upright image (EXIF applied), so GT and prediction boxes line up on
    EXIF-rotated fixtures."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for target in gt.targets:
        boxes = []
        for p in (p for p in pairs if p.image == target.file):
            boxes.append(OverlayBox(box=p.gt_box, label=f"GT {p.field}", kind="accepted"))
            if p.pred_box is not None:
                label = f"{p.field} iou={p.iou:.2f} {p.source}"
                boxes.append(OverlayBox(box=p.pred_box, label=label, kind="fallback"))
        jpeg = render_overlay(gt.path(target).read_bytes(), boxes)
        (out_dir / target.file).write_bytes(jpeg)


def discover_fixture_sets(explicit: list[Path] | None) -> list[Path]:
    if explicit:
        return explicit
    sets = [EVAL_DIR / "fixtures" / "ground_truth.json"]
    private = EVAL_DIR / "fixtures_private" / "ground_truth.json"
    if private.is_file():  # gitignored full-frame set, if the developer has one
        sets.append(private)
    return sets


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Stage B detection eval.")
    parser.add_argument("--backend", choices=sorted(BACKENDS), default="reference")
    parser.add_argument("--iou-threshold", type=float, default=0.5)
    parser.add_argument(
        "--gt",
        type=Path,
        action="append",
        help="ground_truth.json to evaluate (repeatable). Default: committed fixtures, "
        "plus eval/fixtures_private/ground_truth.json when present.",
    )
    parser.add_argument("--out", type=Path, default=EVAL_DIR / "out")
    args = parser.parse_args()

    if args.backend == "gemini":
        reason = gemini_unavailable_reason()
        if reason:
            print(f"gemini backend skipped: {reason}")
            return

    for gt_path in discover_fixture_sets(args.gt):
        gt = load_ground_truth(gt_path)
        fields = template_fields(gt)
        results = BACKENDS[args.backend](gt, fields)
        pairs, canvases, notes = score(gt, fields, results, args.iou_threshold)
        set_name = gt_path.parent.name
        metrics = report(args.backend, gt, pairs, canvases, notes, results, args.iou_threshold)
        out_dir = args.out / args.backend / set_name
        write_overlays(gt, pairs, out_dir / "overlays")
        (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
        # Wall-clock timing varies run to run, so it stays out of metrics.json
        # (which must be bit-identical across runs for deterministic backends).
        timing = {f: round(r.latency_s, 6) for f, r in results.items()}
        (out_dir / "timing.json").write_text(json.dumps({"latency_s": timing}, indent=2))
        print(f"\nwrote {out_dir / 'metrics.json'}, timing.json and {len(gt.targets)} overlays\n")


if __name__ == "__main__":
    main()
