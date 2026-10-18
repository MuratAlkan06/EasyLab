"""Deterministic reference-box transfer for Stage B (ADR-007, Slice 3).

Stage B used to ask Gemini where each annotated field is. This module finds it
geometrically instead. Given the user's reference image, one batch image (both
decoded UPRIGHT, EXIF applied) and the normalized reference boxes:

1. Both images go to grayscale with the longest edge at ``WORK_EDGE_PX``.
2. AKAZE features. Reference-side features are kept only inside the union of
   the reference boxes dilated ``FEATURE_MASK_DILATION``x (A7), so a static
   background plane cannot win RANSAC.
3. Brute-force Hamming 2-NN with Lowe's ratio test, then a homography from
   legacy ``cv2.RANSAC`` (never USAC). The RNG is seeded inside each
   synchronous entry point because OpenCV's RNG is per thread and Stage B runs
   these calls on a reused ``asyncio.to_thread`` pool (A5).
4. Sanity gate on the projected region of interest (A7): convex, not flipped,
   scale 0.5-2.0, rotation <= 30 deg, displacement <= 25% of the diagonal.
5. Per field, the box is projected through H (axis-aligned bounds) and checked
   with NCC (TM_CCOEFF_NORMED, translation only). The template is the box
   padded by 50% of its size on each side, so the bezel ring (~75% of the
   area) dominates and digit changes do not (A8):
     * strong H: NCC only verifies. The box is always H's projection; a gated
       pass raises confidence and a fail keeps the strong-H band.
     * weak or no H: NCC around the raw reference location is a rescue rung,
       and a gated pass moves the reference box by the NCC shift.
   If nothing passes, the field falls back to the reference box.

Confidence bands depend on H strength, not on NCC (A9). The fallback band is
Stage B's ``_REFERENCE_BOX_CONFIDENCE``, which can never reach "ok".

Pure CV on bytes and arrays: no ``app.settings``, no network, no database
(A10). Both entry points block; async callers wrap them in
``asyncio.to_thread``.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping
from dataclasses import dataclass

import cv2
import numpy as np

from app.pipeline.image_utils import open_upright

Box = dict[str, float]
_Rect = tuple[float, float, float, float]  # (x0, y0, x1, y1) in working pixels

# Working resolution: longest edge of both grayscale images (up- or downscaled).
WORK_EDGE_PX = 1024

# A5: seeded at the top of every synchronous entry point.
RNG_SEED = 20261018

# Matching + robust estimation (legacy RANSAC only, A5).
LOWE_RATIO = 0.75
MIN_MATCHES = 8  # findHomography needs 4; fewer than this is noise
RANSAC_REPROJ_PX = 3.0  # OpenCV's default threshold, in working pixels
RANSAC_MAX_ITERS = 2000
RANSAC_CONFIDENCE = 0.995

# A7: reference feature mask + homography sanity gate.
FEATURE_MASK_DILATION = 2.5  # each box scaled 2.5x about its center
H_MIN_SCALE = 0.5
H_MAX_SCALE = 2.0
H_MAX_ROTATION_DEG = 30.0
H_MAX_DISPLACEMENT = 0.25  # fraction of the target diagonal

# A8: H strength + NCC structural rule.
STRONG_MIN_INLIERS = 30
STRONG_MIN_INLIER_RATIO = 0.4
NCC_CONTEXT_PAD = 0.5  # template = box + 50% of its w/h on each side
NCC_SEARCH_PAD = 1.0  # search window = box + 100% of its w/h on each side
NCC_MIN_PEAK = 0.50
NCC_MAX_SHIFT = 0.3  # accepted shift <= 0.3 x min(box w, box h)
_NCC_MIN_TEMPLATE_STD = 1.0  # numerical guard: a flat template has no correlation

# A9: box_confidence bands, keyed to H strength.
STRONG_H_CONFIDENCE = 0.85
STRONG_H_NCC_CONFIDENCE = 0.93  # A9 allows 0.92-0.95; flat because NCC does not set the band
NCC_RESCUE_CONFIDENCE = 0.70
FALLBACK_CONFIDENCE = 0.55  # == stage_b._REFERENCE_BOX_CONFIDENCE (pinned by a test)

METHOD_HOMOGRAPHY = "homography"
METHOD_HOMOGRAPHY_NCC = "homography+ncc"
METHOD_NCC_RESCUE = "ncc_rescue"
METHOD_FALLBACK = "fallback"


@dataclass(frozen=True)
class AlignedBox:
    """One field's transferred box (normalized 0..1, upright target space).

    ``ncc_peak`` and ``ncc_shift`` describe the NCC search before the gate is
    applied (``ncc_shift`` is the best shift's length / min(box w, box h)).
    They are ``None`` when NCC could not run.
    """

    box: Box
    box_confidence: float
    method: str
    ncc_peak: float | None = None
    ncc_shift: float | None = None


@dataclass(frozen=True, eq=False)
class ReferenceFeatures:
    """Reference image state reused for every target in a job.

    Holds only the working-resolution grayscale and the masked features. The
    full-resolution decode is released when ``prepare_reference`` returns.
    """

    gray: np.ndarray
    boxes: dict[Hashable, Box]
    roi: _Rect  # bounds of the feature mask: where H is estimated and gated
    points: np.ndarray  # (N, 2) float32 keypoint coordinates
    descriptors: np.ndarray | None  # (N, 61) uint8 AKAZE descriptors


@dataclass(frozen=True, eq=False)
class AlignResult:
    fields: dict[Hashable, AlignedBox]
    h_status: str  # "strong" | "weak" | "rejected:<check>" | "none"
    matches: int  # ratio-test survivors
    inliers: int  # RANSAC inliers (0 when no H was estimated)
    homography: np.ndarray | None  # 3x3, reference -> target working pixels


# --------------------------------------------------------------------------- #
# Entry points (synchronous; run them via asyncio.to_thread)
# --------------------------------------------------------------------------- #


def prepare_reference(
    image_bytes: bytes, boxes: Mapping[Hashable, Box | None]
) -> ReferenceFeatures:
    """Decode the reference once and compute its masked features.

    ``boxes`` maps a caller-chosen key (field id or name) to the normalized
    upright reference box. Fields without a box are skipped, and
    ``align_target`` returns nothing for them.
    """
    cv2.setRNGSeed(RNG_SEED)
    gray = _load_gray(image_bytes)
    height, width = gray.shape
    usable = {key: dict(box) for key, box in boxes.items() if box}

    mask = np.zeros((height, width), np.uint8)
    for box in usable.values():
        x0, y0, x1, y1 = _pad_rect(_box_px(box, width, height), _dilation_pad(), width, height)
        mask[y0:y1, x0:x1] = 255
    ys, xs = np.nonzero(mask)
    roi: _Rect = (
        (float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1))
        if xs.size
        else (0.0, 0.0, float(width), float(height))
    )
    points, descriptors = _detect(gray, mask)
    return ReferenceFeatures(gray, usable, roi, points, descriptors)


def align_target(reference: ReferenceFeatures, image_bytes: bytes) -> AlignResult:
    """Transfer every reference box onto one target image."""
    cv2.setRNGSeed(RNG_SEED)
    target = _load_gray(image_bytes)
    t_points, t_desc = _detect(target, None)
    src, dst = _match(reference, t_points, t_desc)

    homography: np.ndarray | None = None
    inliers = 0
    if len(src) >= MIN_MATCHES:
        h, inlier_mask = cv2.findHomography(
            src,
            dst,
            cv2.RANSAC,
            RANSAC_REPROJ_PX,
            maxIters=RANSAC_MAX_ITERS,
            confidence=RANSAC_CONFIDENCE,
        )
        if h is not None and h.shape == (3, 3) and np.isfinite(h).all():
            homography = h
            inliers = int(inlier_mask.sum())

    if homography is None:
        h_status = "none"
    else:
        problem = check_homography(homography, reference.roi, reference.gray.shape, target.shape)
        if problem is not None:
            h_status = f"rejected:{problem}"
        elif inliers >= STRONG_MIN_INLIERS and inliers / len(src) >= STRONG_MIN_INLIER_RATIO:
            h_status = "strong"
        else:
            h_status = "weak"

    if h_status == "strong":
        assert homography is not None
        fields = _strong_fields(reference, target, homography)
    else:
        fields = _rescue_fields(reference, target)
    return AlignResult(fields, h_status, len(src), inliers, homography)


# --------------------------------------------------------------------------- #
# Homography sanity gate (A7)
# --------------------------------------------------------------------------- #


def check_homography(
    homography: np.ndarray,
    roi: _Rect,
    ref_shape: tuple[int, ...],
    target_shape: tuple[int, ...],
) -> str | None:
    """Return ``None`` if H is plausible over ``roi``, else the failed check.

    Checks the projected ROI quad: convex and not mirrored, scale (sqrt of the
    area ratio) in [0.5, 2.0], rotation of its horizontal axis <= 30 deg, and
    center displacement <= 25% of the target diagonal. Displacement is
    measured against the same normalized position (the identity transfer).
    """
    x0, y0, x1, y1 = roi
    quad = _transform(homography, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    if not np.isfinite(quad).all():
        return "degenerate"
    for i in range(4):
        a, b, c = quad[i], quad[(i + 1) % 4], quad[(i + 2) % 4]
        # Source corners run TL->TR->BR->BL: every turn is positive (y down).
        if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) <= 0:
            return "not_convex_or_flipped"

    xs, ys = quad[:, 0], quad[:, 1]
    area = 0.5 * abs(float(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))))
    scale = math.sqrt(area / ((x1 - x0) * (y1 - y0)))
    if not H_MIN_SCALE <= scale <= H_MAX_SCALE:
        return "scale"

    left, right = (quad[0] + quad[3]) / 2, (quad[1] + quad[2]) / 2
    rotation = math.degrees(math.atan2(right[1] - left[1], right[0] - left[0]))
    if abs(rotation) > H_MAX_ROTATION_DEG:
        return "rotation"

    ref_h, ref_w = ref_shape[:2]
    tgt_h, tgt_w = target_shape[:2]
    expected_x = (x0 + x1) / 2 / ref_w * tgt_w
    expected_y = (y0 + y1) / 2 / ref_h * tgt_h
    center = quad.mean(axis=0)
    displacement = math.hypot(center[0] - expected_x, center[1] - expected_y)
    if displacement > H_MAX_DISPLACEMENT * math.hypot(tgt_w, tgt_h):
        return "displacement"
    return None


# --------------------------------------------------------------------------- #
# Per-field rungs
# --------------------------------------------------------------------------- #


def _strong_fields(
    reference: ReferenceFeatures, target: np.ndarray, homography: np.ndarray
) -> dict[Hashable, AlignedBox]:
    """Strong H: project each box, and let NCC verify it in the reference frame."""
    ref_h, ref_w = reference.gray.shape
    tgt_h, tgt_w = target.shape
    # Target resampled into the reference frame, so NCC is a pure translation
    # search even when the camera zoomed or rolled.
    rectified = cv2.warpPerspective(
        target,
        homography,
        (ref_w, ref_h),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    out: dict[Hashable, AlignedBox] = {}
    for key, box in reference.boxes.items():
        rect = _box_px(box, ref_w, ref_h)
        h_box = _project_box(homography, rect, tgt_w, tgt_h)
        if h_box is None:  # projects entirely off the target
            out[key] = AlignedBox(dict(box), FALLBACK_CONFIDENCE, METHOD_FALLBACK)
            continue
        ncc = _ncc(reference.gray, rectified, rect)
        peak, shift, accepted = ncc if ncc else (None, None, None)
        if accepted is not None:
            # Verification only: NCC agrees with H, so raise confidence. The box stays H's.
            out[key] = AlignedBox(
                h_box, STRONG_H_NCC_CONFIDENCE, METHOD_HOMOGRAPHY_NCC, peak, shift
            )
        else:
            # Glare or a digit change is not misalignment: keep H's box and band.
            out[key] = AlignedBox(h_box, STRONG_H_CONFIDENCE, METHOD_HOMOGRAPHY, peak, shift)
    return out


def _rescue_fields(reference: ReferenceFeatures, target: np.ndarray) -> dict[Hashable, AlignedBox]:
    """Weak or no H: NCC around the raw reference location, else fallback."""
    ref_h, ref_w = reference.gray.shape
    # Same normalized coordinates as the reference (identity transfer).
    view = target if target.shape == reference.gray.shape else cv2.resize(target, (ref_w, ref_h))
    out: dict[Hashable, AlignedBox] = {}
    for key, box in reference.boxes.items():
        ncc = _ncc(reference.gray, view, _box_px(box, ref_w, ref_h))
        peak, shift, accepted = ncc if ncc else (None, None, None)
        if accepted is not None:
            dx, dy = accepted
            moved = _clip_box(
                box["x"] + dx / ref_w,
                box["y"] + dy / ref_h,
                box["x"] + box["width"] + dx / ref_w,
                box["y"] + box["height"] + dy / ref_h,
            )
            if moved is not None:
                out[key] = AlignedBox(moved, NCC_RESCUE_CONFIDENCE, METHOD_NCC_RESCUE, peak, shift)
                continue
        out[key] = AlignedBox(dict(box), FALLBACK_CONFIDENCE, METHOD_FALLBACK, peak, shift)
    return out


def _ncc(
    ref_gray: np.ndarray, search: np.ndarray, rect: _Rect
) -> tuple[float, float, tuple[float, float] | None] | None:
    """Context-template NCC of ``rect`` (reference frame) within ``search``.

    ``search`` must be in the reference frame (same shape as ``ref_gray``).
    Returns ``(peak, shift, accepted)``: ``shift`` is the best translation's
    length / min(box w, box h), and ``accepted`` is that ``(dx, dy)`` in
    working pixels if it passes the A8 gate, else ``None``. Returns ``None``
    when NCC cannot run (box clipped away, flat template).
    """
    height, width = ref_gray.shape
    tx0, ty0, tx1, ty1 = _pad_rect(rect, NCC_CONTEXT_PAD, width, height)
    sx0, sy0, sx1, sy1 = _pad_rect(rect, NCC_SEARCH_PAD, width, height)
    template = ref_gray[ty0:ty1, tx0:tx1]
    window = search[sy0:sy1, sx0:sx1]
    if (
        template.size == 0
        or window.shape[0] < template.shape[0]
        or window.shape[1] < template.shape[1]
        or float(template.std()) < _NCC_MIN_TEMPLATE_STD
    ):
        return None
    scores = cv2.matchTemplate(window, template, cv2.TM_CCOEFF_NORMED)
    scores = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
    _, peak, _, (mx, my) = cv2.minMaxLoc(scores)
    dx, dy = float(sx0 + mx - tx0), float(sy0 + my - ty0)
    min_dim = min(rect[2] - rect[0], rect[3] - rect[1])
    shift = math.hypot(dx, dy) / min_dim
    passed = peak >= NCC_MIN_PEAK and shift <= NCC_MAX_SHIFT
    return float(peak), round(shift, 6), (dx, dy) if passed else None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_gray(image_bytes: bytes) -> np.ndarray:
    """Upright grayscale, longest edge exactly ``WORK_EDGE_PX``."""
    gray = np.array(open_upright(image_bytes).convert("L"))
    height, width = gray.shape
    scale = WORK_EDGE_PX / max(width, height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    return cv2.resize(gray, size, interpolation=interpolation)


def _detect(gray: np.ndarray, mask: np.ndarray | None) -> tuple[np.ndarray, np.ndarray | None]:
    akaze = cv2.AKAZE_create()  # per call: Feature2D objects are not shared across threads
    keypoints, descriptors = akaze.detectAndCompute(gray, mask)
    points = np.array([kp.pt for kp in keypoints], dtype=np.float32).reshape(-1, 2)
    return points, descriptors


def _match(
    reference: ReferenceFeatures, t_points: np.ndarray, t_desc: np.ndarray | None
) -> tuple[np.ndarray, np.ndarray]:
    """Ratio-test matches as (src, dst) point arrays shaped (N, 1, 2)."""
    empty = np.empty((0, 1, 2), np.float32)
    if reference.descriptors is None or t_desc is None or len(t_desc) < 2:
        return empty, empty
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(reference.descriptors, t_desc, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < LOWE_RATIO * p[1].distance]
    if not good:
        return empty, empty
    src = reference.points[[m.queryIdx for m in good]].reshape(-1, 1, 2)
    dst = t_points[[m.trainIdx for m in good]].reshape(-1, 1, 2)
    return src, dst


def _box_px(box: Box, width: int, height: int) -> _Rect:
    x0, y0 = box["x"] * width, box["y"] * height
    return x0, y0, x0 + box["width"] * width, y0 + box["height"] * height


def _dilation_pad() -> float:
    """Per-side pad that scales a box ``FEATURE_MASK_DILATION``x about its center."""
    return (FEATURE_MASK_DILATION - 1.0) / 2.0


def _pad_rect(rect: _Rect, pad: float, width: int, height: int) -> tuple[int, int, int, int]:
    """Grow ``rect`` by ``pad`` x its own w/h on each side; integer, clipped."""
    x0, y0, x1, y1 = rect
    px, py = (x1 - x0) * pad, (y1 - y0) * pad
    return (
        max(0, math.floor(x0 - px)),
        max(0, math.floor(y0 - py)),
        min(width, math.ceil(x1 + px)),
        min(height, math.ceil(y1 + py)),
    )


def _transform(homography: np.ndarray, points: list[tuple[float, float]]) -> np.ndarray:
    pts = np.array(points, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, homography).reshape(-1, 2)


def _project_box(homography: np.ndarray, rect: _Rect, width: int, height: int) -> Box | None:
    """Axis-aligned bounds of ``rect`` mapped through H, normalized to the target."""
    x0, y0, x1, y1 = rect
    quad = _transform(homography, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    if not np.isfinite(quad).all():
        return None
    (qx0, qy0), (qx1, qy1) = quad.min(axis=0), quad.max(axis=0)
    return _clip_box(qx0 / width, qy0 / height, qx1 / width, qy1 / height)


def _clip_box(x0: float, y0: float, x1: float, y1: float) -> Box | None:
    """Normalized corners -> clipped ``{"x","y","width","height"}`` (plain floats)."""
    x0, y0 = min(max(float(x0), 0.0), 1.0), min(max(float(y0), 0.0), 1.0)
    x1, y1 = min(max(float(x1), 0.0), 1.0), min(max(float(y1), 0.0), 1.0)
    if x1 <= x0 or y1 <= y0:
        return None
    return {
        "x": round(x0, 6),
        "y": round(y0, 6),
        "width": round(x1 - x0, 6),
        "height": round(y1 - y0, 6),
    }
