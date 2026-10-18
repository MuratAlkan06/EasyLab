"""Deterministic reference-box transfer (ADR-007 Slice 3, ``app.pipeline.align``).

Scenes are generated in memory from seeded numpy RNGs (blobs on grey) and
encoded as lossless PNG, so every expected box follows exactly from a known
transform. The amendment tests (A5, A7, A8, A9, A10) each include a
self-binding check: they re-run the same input with the guard disabled and
assert the outcome changes, so the guard is shown to carry the result.
"""

from __future__ import annotations

import math
import subprocess
import sys
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.pipeline import align, stage_b
from app.pipeline.confidence import compute_cell_status, compute_combined_confidence
from app.pipeline.crop_utils import box_iou

WORKER_DIR = Path(__file__).resolve().parents[2]
W, H = 900, 600  # synthetic scene size (px); align works at 1024 longest edge
STRONG_METHODS = {align.METHOD_HOMOGRAPHY, align.METHOD_HOMOGRAPHY_NCC}

# Normalized boxes for three "instruments" spread over the scene.
FIELDS = {
    "left": {"x": 60 / W, "y": 70 / H, "width": 150 / W, "height": 60 / H},
    "center": {"x": 380 / W, "y": 280 / H, "width": 150 / W, "height": 56 / H},
    "right": {"x": 640 / W, "y": 420 / H, "width": 170 / W, "height": 90 / H},
}


# --------------------------------------------------------------------------- #
# Scene helpers
# --------------------------------------------------------------------------- #


def _texture(height: int, width: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.full((height, width), 128, np.uint8)
    for _ in range(height * width // 300):
        x, y = int(rng.integers(0, width)), int(rng.integers(0, height))
        size = int(rng.integers(3, 24))
        color = int(rng.integers(0, 256))
        if rng.random() < 0.5:
            cv2.rectangle(img, (x, y), (x + size, y + int(rng.integers(3, 24))), color, -1)
        else:
            cv2.circle(img, (x, y), size // 2 + 1, color, -1)
    return cv2.GaussianBlur(img, (3, 3), 0)


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _px(box: dict) -> tuple[int, int, int, int]:
    x0, y0 = round(box["x"] * W), round(box["y"] * H)
    return x0, y0, x0 + round(box["width"] * W), y0 + round(box["height"] * H)


def _warp(img: np.ndarray, m: np.ndarray) -> np.ndarray:
    return cv2.warpAffine(img, m, (W, H), flags=cv2.INTER_LINEAR, borderValue=128)


def _expected(box: dict, m: np.ndarray) -> dict:
    """AABB of the box corners under the 2x3 affine ``m``, normalized."""
    x0, y0, x1, y1 = (
        box["x"] * W,
        box["y"] * H,
        (box["x"] + box["width"]) * W,
        (box["y"] + box["height"]) * H,
    )
    pts = np.array([[x0, y0, 1], [x1, y0, 1], [x1, y1, 1], [x0, y1, 1]]).T
    q = m @ pts
    qx0, qy0, qx1, qy1 = q[0].min(), q[1].min(), q[0].max(), q[1].max()
    return {"x": qx0 / W, "y": qy0 / H, "width": (qx1 - qx0) / W, "height": (qy1 - qy0) / H}


def _center_px(box: dict) -> tuple[float, float]:
    return (box["x"] + box["width"] / 2) * W, (box["y"] + box["height"] / 2) * H


def _similarity(*, angle: float = 0.0, zoom: float = 1.0, dx: float = 0.0, dy: float = 0.0):
    m = cv2.getRotationMatrix2D((W / 2, H / 2), angle, zoom)
    m[:, 2] += (dx, dy)
    return m


_SEGMENTS = {  # 7-segment glyphs: a b c d e f g
    "8": "abcdefg",
    "1": "bc",
}


def _draw_display(img: np.ndarray, box: dict, digits: str, offset: tuple[int, int] = (0, 0)):
    """An LCD instrument: textured bezel panel (2.5x the box), pale glass, segment digits."""
    ox, oy = offset
    x0, y0, x1, y1 = _px(box)
    x0, y0, x1, y1 = x0 + ox, y0 + oy, x1 + ox, y1 + oy
    bw, bh = x1 - x0, y1 - y0
    px0, py0 = int(x0 - 0.75 * bw), int(y0 - 0.75 * bh)
    px1, py1 = int(x1 + 0.75 * bw), int(y1 + 0.75 * bh)
    panel = _texture(py1 - py0, px1 - px0, seed=7) // 2 + 20  # dark bezel with print/buttons
    img[py0:py1, px0:px1] = panel
    cv2.rectangle(img, (x0, y0), (x1, y1), 200, -1)  # glass
    cell = bw // len(digits)
    for i, ch in enumerate(digits):
        cx0, cx1 = x0 + i * cell + cell // 5, x0 + (i + 1) * cell - cell // 5
        cy0, cy1, cym = y0 + bh // 6, y1 - bh // 6, (y0 + y1) // 2
        t = max(2, cell // 8)
        seg = {
            "a": (cx0, cy0, cx1, cy0 + t),
            "b": (cx1 - t, cy0, cx1, cym),
            "c": (cx1 - t, cym, cx1, cy1),
            "d": (cx0, cy1 - t, cx1, cy1),
            "e": (cx0, cym, cx0 + t, cy1),
            "f": (cx0, cy0, cx0 + t, cym),
            "g": (cx0, cym - t // 2, cx1, cym + t // 2),
        }
        for s in _SEGMENTS[ch]:
            sx0, sy0, sx1, sy1 = seg[s]
            cv2.rectangle(img, (sx0, sy0), (sx1, sy1), 30, -1)


@pytest.fixture(scope="module")
def scene() -> np.ndarray:
    return _texture(H, W, seed=1)


@pytest.fixture(scope="module")
def reference(scene: np.ndarray) -> align.ReferenceFeatures:
    return align.prepare_reference(_png(scene), FIELDS)


# --------------------------------------------------------------------------- #
# Synthetic transforms: projected boxes match the known geometry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "transform",
    [
        pytest.param(_similarity(dx=35, dy=-22), id="shift"),
        pytest.param(_similarity(zoom=1.15), id="zoom-in"),
        pytest.param(_similarity(zoom=0.85), id="zoom-out"),
        pytest.param(_similarity(angle=8), id="rotate"),
        pytest.param(_similarity(angle=-5, zoom=0.9, dx=20, dy=15), id="combined"),
    ],
)
def test_known_transform_projects_boxes(scene, reference, transform) -> None:
    result = align.align_target(reference, _png(_warp(scene, transform)))

    assert result.h_status == "strong"
    for name, box in FIELDS.items():
        got = result.fields[name]
        assert got.method in STRONG_METHODS
        assert got.box_confidence in (align.STRONG_H_CONFIDENCE, align.STRONG_H_NCC_CONFIDENCE)
        assert box_iou(got.box, _expected(box, transform)) >= 0.9, name
        assert all(type(v) is float for v in got.box.values())  # JSON-safe for Stage C


def test_identical_image_is_strong_and_ncc_confirmed(scene, reference) -> None:
    result = align.align_target(reference, _png(scene))
    for name, box in FIELDS.items():
        got = result.fields[name]
        assert got.method == align.METHOD_HOMOGRAPHY_NCC
        assert got.box_confidence == align.STRONG_H_NCC_CONFIDENCE
        assert box_iou(got.box, box) >= 0.97


def test_fields_without_a_reference_box_are_skipped(scene) -> None:
    ref = align.prepare_reference(_png(scene), {"left": FIELDS["left"], "gone": None})
    assert set(align.align_target(ref, _png(scene)).fields) == {"left"}


def test_featureless_target_falls_back_with_reference_box(reference) -> None:
    flat = np.full((H, W), 128, np.uint8)
    result = align.align_target(reference, _png(flat))
    assert result.h_status == "none"
    for name, box in FIELDS.items():
        got = result.fields[name]
        assert got.method == align.METHOD_FALLBACK
        assert got.box == box
        assert got.box_confidence == align.FALLBACK_CONFIDENCE


# --------------------------------------------------------------------------- #
# A5: per-call RNG seeding -> identical H + boxes on different threads
# --------------------------------------------------------------------------- #


def test_same_alignment_on_two_threads_is_identical(scene, reference) -> None:
    target = _png(_warp(scene, _similarity(angle=-5, zoom=0.9, dx=20, dy=15)))
    barrier = threading.Barrier(2)  # both alive at once -> distinct thread idents
    results: dict[int, align.AlignResult] = {}
    idents: dict[int, int] = {}

    def run(slot: int) -> None:
        barrier.wait()
        cv2.setRNGSeed(1000 + slot)  # leave each thread's OpenCV RNG in a different state
        cv2.randu(np.empty((64, 64), np.float32), 0, 1)
        results[slot] = align.align_target(reference, target)
        idents[slot] = threading.get_ident()

    threads = [threading.Thread(target=run, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert idents[0] != idents[1]
    a, b = results[0], results[1]
    assert a.homography is not None and np.array_equal(a.homography, b.homography)
    assert (a.matches, a.inliers, a.h_status) == (b.matches, b.inliers, b.h_status)
    assert a.fields == b.fields
    main = align.align_target(reference, target)  # and the calling thread agrees
    assert np.array_equal(main.homography, a.homography) and main.fields == a.fields


def _eval_ground_truth():
    sys.path.insert(0, str(WORKER_DIR / "eval"))
    from gt_io import load_ground_truth

    return load_ground_truth(WORKER_DIR / "eval/fixtures/ground_truth.json")


@pytest.mark.parametrize("target_file", ["target_06.jpg", "target_08.jpg"])
def test_real_fixture_alignment_is_identical_across_pool_threads(target_file) -> None:
    """Production shape: a reused thread pool, many runs. Real parallax data is
    where a nondeterministic estimator shows (USAC_PARALLEL gives 3 distinct
    H in 16 runs on these two fixtures); legacy RANSAC must give exactly one."""
    from concurrent.futures import ThreadPoolExecutor

    gt = _eval_ground_truth()
    ref = align.prepare_reference(gt.path(gt.reference).read_bytes(), gt.reference.boxes)
    raw = (gt.root / target_file).read_bytes()
    with ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(lambda _: align.align_target(ref, raw), range(12)))
    assert len({r.homography.tobytes() for r in runs}) == 1
    assert all(r.fields == runs[0].fields for r in runs)


def test_entry_points_seed_the_rng_and_use_legacy_ransac() -> None:
    source = (WORKER_DIR / "app/pipeline/align.py").read_text()
    assert source.count("cv2.setRNGSeed(RNG_SEED)") == 2  # prepare_reference + align_target
    assert "cv2.RANSAC" in source and "USAC" not in source.replace("never USAC", "")


# --------------------------------------------------------------------------- #
# A7: masked reference features + H sanity gate
# --------------------------------------------------------------------------- #


_GAUGE = {"x": 350 / W, "y": 250 / H, "width": 100 / W, "height": 50 / H}


def _gauge_panel() -> tuple[int, int, int, int]:
    """Instrument body: the display padded 80% per side, i.e. just beyond the
    2.5x A7 feature mask, so every masked reference feature is on the instrument."""
    x0, y0, x1, y1 = _px(_GAUGE)
    bw, bh = x1 - x0, y1 - y0
    return int(x0 - 0.8 * bw), int(y0 - 0.8 * bh), int(x1 + 0.8 * bw), int(y1 + 0.8 * bh)


def _instrument_scene() -> np.ndarray:
    """Textured static background with one instrument panel around the gauge."""
    img = _texture(H, W, seed=3)
    px0, py0, px1, py1 = _gauge_panel()
    img[py0:py1, px0:px1] = _texture(py1 - py0, px1 - px0, seed=11)
    return img


def _occlude(img: np.ndarray, kind: str) -> np.ndarray:
    out = img.copy()
    px0, py0, px1, py1 = _gauge_panel()
    patch = (
        np.full((py1 - py0, px1 - px0), 90, np.uint8)
        if kind == "flat"
        else _texture(py1 - py0, px1 - px0, seed=99)
    )
    out[py0:py1, px0:px1] = patch
    return out


@pytest.mark.parametrize("kind", ["flat", "background-texture"])
def test_occluded_instrument_falls_back_never_background_anchored(monkeypatch, kind) -> None:
    ref_img = _instrument_scene()
    target = _png(_occlude(ref_img, kind))

    result = align.align_target(align.prepare_reference(_png(ref_img), {"gauge": _GAUGE}), target)
    got = result.fields["gauge"]
    assert result.h_status != "strong"
    assert got.method == align.METHOD_FALLBACK
    assert got.box == _GAUGE and got.box_confidence == align.FALLBACK_CONFIDENCE

    # Binding: without the A7 mask (dilation covering the whole frame) the static
    # background wins RANSAC and an accepted box lands on the occluder.
    monkeypatch.setattr(align, "FEATURE_MASK_DILATION", 50.0)
    unmasked = align.align_target(align.prepare_reference(_png(ref_img), {"gauge": _GAUGE}), target)
    assert unmasked.h_status == "strong"
    assert unmasked.fields["gauge"].method in STRONG_METHODS


def _roi() -> tuple[float, float, float, float]:
    return 200.0, 150.0, 600.0, 450.0  # 400x300 in an 800x600 frame


def _about_roi_center(m: np.ndarray) -> np.ndarray:
    cx, cy = 400.0, 300.0
    to = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]], float)
    back = np.array([[1, 0, cx], [0, 1, cy], [0, 0, 1]], float)
    return back @ m @ to


def _rot(deg: float, scale: float = 1.0) -> np.ndarray:
    r = math.radians(deg)
    c, s = math.cos(r) * scale, math.sin(r) * scale
    return _about_roi_center(np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], float))


@pytest.mark.parametrize(
    ("homography", "problem"),
    [
        pytest.param(np.eye(3), None, id="identity"),
        pytest.param(_rot(25, 1.8), None, id="within-limits"),
        pytest.param(np.array([[1, 0, 190.0], [0, 1, 0], [0, 0, 1]]), None, id="shift-19pct"),
        pytest.param(_rot(31), "rotation", id="rotation-31"),
        pytest.param(_rot(-45), "rotation", id="rotation-neg45"),
        pytest.param(_rot(0, 2.2), "scale", id="scale-2.2"),
        pytest.param(_rot(0, 0.45), "scale", id="scale-0.45"),
        pytest.param(
            np.array([[1, 0, 260.0], [0, 1, 0], [0, 0, 1]]), "displacement", id="shift-26pct"
        ),
        pytest.param(
            _about_roi_center(np.diag([-1.0, 1.0, 1.0])), "not_convex_or_flipped", id="mirror"
        ),
        pytest.param(  # w = 2 - 0.004x hits 0 at x=500, inside the ROI
            np.array([[1, 0, 0], [0, 1, 0], [-0.004, 0, 2.0]]),
            "not_convex_or_flipped",
            id="past-horizon",
        ),
    ],
)
def test_homography_sanity_gate(homography, problem) -> None:
    assert align.check_homography(homography, _roi(), (600, 800), (600, 800)) == problem


def test_implausible_but_consistent_h_takes_the_next_rung(scene, reference) -> None:
    """A real 45-degree roll matches fine but fails the gate -> no strong-H box."""
    result = align.align_target(reference, _png(_warp(scene, _similarity(angle=45))))
    assert result.h_status == "rejected:rotation"
    assert all(f.method not in STRONG_METHODS for f in result.fields.values())


# --------------------------------------------------------------------------- #
# A8: NCC structural rule
# --------------------------------------------------------------------------- #


def _lcd_scene(digits: str, local_offset: tuple[int, int] = (0, 0)) -> np.ndarray:
    img = _texture(H, W, seed=5)
    _draw_display(img, FIELDS["center"], digits, local_offset)
    return img


@pytest.fixture(scope="module")
def lcd_reference() -> align.ReferenceFeatures:
    return align.prepare_reference(_png(_lcd_scene("8888")), FIELDS)


_GLOBAL = _similarity(dx=24, dy=14)
_GATE_PX = align.NCC_MAX_SHIFT * min(150, 56)  # 0.3 x min(box dims), target px (no zoom)


@pytest.mark.parametrize("digits", ["8888", "1111"])  # 7 segments vs 2: maximally different
@pytest.mark.parametrize("local_dx", [0, 56], ids=["in-place", "beyond-gate"])
def test_ncc_never_moves_a_strong_h_box_beyond_the_shift_gate(
    lcd_reference, digits, local_dx
) -> None:
    target = _png(_warp(_lcd_scene(digits, (local_dx, 0)), _GLOBAL))
    result = align.align_target(lcd_reference, target)
    assert result.h_status == "strong"

    got = result.fields["center"]
    h_box = _expected(FIELDS["center"], _GLOBAL)  # where the (anchor-driven) H puts it
    gx, gy = _center_px(got.box)
    hx, hy = _center_px(h_box)
    assert math.hypot(gx - hx, gy - hy) <= _GATE_PX
    assert got.method in STRONG_METHODS  # NCC never demotes a strong-H box

    if local_dx:
        # Binding: NCC's own best match is beyond the gate (the LCD really moved
        # 56 px on its own), and the gate refused it: no move, no confidence bump.
        assert got.ncc_shift is not None and got.ncc_shift > align.NCC_MAX_SHIFT
        assert got.ncc_peak is not None and got.ncc_peak >= align.NCC_MIN_PEAK
        assert got.method == align.METHOD_HOMOGRAPHY
        assert got.box_confidence == align.STRONG_H_CONFIDENCE
    else:
        assert got.method == align.METHOD_HOMOGRAPHY_NCC  # bezel ring outweighs digits
        assert got.box_confidence == align.STRONG_H_NCC_CONFIDENCE


def test_glare_fails_ncc_but_keeps_the_strong_h_box_and_band(lcd_reference) -> None:
    img = _lcd_scene("8888")
    x0, y0, x1, y1 = _px(FIELDS["center"])
    bw, bh = x1 - x0, y1 - y0
    img[y0 - bh // 2 : y1 + bh // 2, x0 - bw // 2 : x1 + bw // 2] = 250  # blown-out highlight
    got = align.align_target(lcd_reference, _png(_warp(img, _GLOBAL))).fields["center"]
    assert got.method == align.METHOD_HOMOGRAPHY
    assert got.box_confidence == align.STRONG_H_CONFIDENCE
    assert box_iou(got.box, _expected(FIELDS["center"], _GLOBAL)) >= 0.9


@pytest.mark.parametrize(("shift_px", "rescued"), [(10, True), (45, False)])
def test_weak_h_ncc_rescue_is_gated(monkeypatch, scene, shift_px, rescued) -> None:
    """Weak H: NCC around the raw reference location rescues small shifts only
    (gate = 0.3 x 56 px = 16.8 px; 45 px is inside the search window but beyond it)."""
    monkeypatch.setattr(align, "STRONG_MIN_INLIERS", 10**9)  # force the weak-H branch
    ref = align.prepare_reference(_png(scene), {"center": FIELDS["center"]})
    m = _similarity(dx=shift_px)
    result = align.align_target(ref, _png(_warp(scene, m)))
    assert result.h_status == "weak"
    got = result.fields["center"]
    if rescued:
        assert got.method == align.METHOD_NCC_RESCUE
        assert got.box_confidence == align.NCC_RESCUE_CONFIDENCE
        assert box_iou(got.box, _expected(FIELDS["center"], m)) >= 0.95
    else:
        assert got.ncc_shift is not None and got.ncc_shift > align.NCC_MAX_SHIFT
        assert got.method == align.METHOD_FALLBACK
        assert got.box == FIELDS["center"]


# --------------------------------------------------------------------------- #
# A9: confidence bands vs cell status
# --------------------------------------------------------------------------- #


def _status(box_conf: float, value_conf: float) -> str:
    return compute_cell_status(
        combined_confidence=compute_combined_confidence(box_conf, value_conf),
        legible=True,
        validation_error=None,
        found=True,
        parsed_value_is_null=False,
    )


def test_fallback_band_is_stage_b_reference_confidence() -> None:
    assert align.FALLBACK_CONFIDENCE == stage_b._REFERENCE_BOX_CONFIDENCE


def test_bands_are_ordered_and_in_range() -> None:
    assert (
        align.FALLBACK_CONFIDENCE
        < align.NCC_RESCUE_CONFIDENCE
        < align.STRONG_H_CONFIDENCE
        < align.STRONG_H_NCC_CONFIDENCE
    )
    assert 0.92 <= align.STRONG_H_NCC_CONFIDENCE <= 0.95


@pytest.mark.parametrize("value_conf", [i / 100 for i in range(101)])
def test_fallback_cells_never_reach_ok(value_conf) -> None:
    assert _status(align.FALLBACK_CONFIDENCE, value_conf) in ("needs_review", "low_confidence")


@pytest.mark.parametrize(
    "box_conf", [align.STRONG_H_CONFIDENCE, align.STRONG_H_NCC_CONFIDENCE], ids=["H", "H+NCC"]
)
def test_strong_h_with_clean_read_reaches_ok(box_conf) -> None:
    assert _status(box_conf, 0.85) == "ok"


def test_aligned_eval_fixtures_get_strong_bands() -> None:
    """A9 Accept+: every field of the committed aligned-class fixtures lands in a
    strong-H band, so a clean Stage C read (>= 0.85) makes the cell "ok"."""
    gt = _eval_ground_truth()
    ref = align.prepare_reference(gt.path(gt.reference).read_bytes(), gt.reference.boxes)
    aligned = [t for t in gt.targets if t.cls == "aligned"]
    assert aligned
    for target in aligned:
        result = align.align_target(ref, gt.path(target).read_bytes())
        for name, got in result.fields.items():
            assert got.method in STRONG_METHODS, (target.file, name)
            assert _status(got.box_confidence, 0.85) == "ok"


# --------------------------------------------------------------------------- #
# A10: pure module
# --------------------------------------------------------------------------- #


def test_align_imports_no_settings_network_or_db() -> None:
    code = (
        "import sys, app.pipeline.align\n"
        "bad = [m for m in ('app.settings', 'supabase', 'asyncpg', 'httpx', 'google.genai')"
        " if m in sys.modules]\n"
        "print(','.join(bad))"
    )
    env = {"PATH": "/usr/bin:/bin"}  # no SUPABASE_* etc.: importing settings would fail
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=WORKER_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == ""
