# ADR-007: Deterministic Stage B detection (reference-box transfer)

- **Status:** Accepted
- **Date:** 2026-10-18
- **Scope:** Stage B (region detection) in `apps/worker`. The full runtime spec is [`docs/ai-pipeline.md` §4](ai-pipeline.md#stage-b).

## Context

Stage B finds each annotated field on every non-reference image so Stage C can crop and read it. Until Phase 7 it was a Gemini 2.5 Pro call per image, and it failed in ways that prompting could not fix:

- **Text-only detection.** The detector got the target image plus a text `semantic_description` and a textual `[ref position]` hint per field. No reference pixels ever reached it. On real bench photos it confidently boxed the printed panel labels (`VOLT`, `AMP`) next to the live LCDs instead of the displays themselves.
- **Prompting was exhausted.** Six prompt-engineering attempts, including explicit "do NOT box static printed text" rules and position anchoring, did not fix the label-vs-display confusion.
- **IoU was logged, never gated.** IoU against the user's `reference_box` was computed for telemetry but played no part in acceptance. A confident wrong box (IoU 0) therefore beat the honest 0.55 `reference_box` fallback every time.
- **EXIF coordinate-space bug.** Preprocessing applied EXIF transpose before the model, so boxes were in upright space. But image dimensions, Stage C crops and debug overlays all opened the *raw* bytes. Orientation-6/8 photos got wrong crops and misleading overlays.
- **Unverifiable.** Detection was stochastic, cost one Pro call per image, and could only be checked with a live API key. No offline accuracy measurement existed.

Batches are near-identical photos of the same setup, so the user's own reference box is the strongest available signal. What was missing was an estimate of how the camera moved between the reference and each image.

## Options considered

| Option | Outcome |
|---|---|
| IoU-gate the Gemini detector | Rejected. The gate converges to "use the reference box anyway" while keeping the cost, the stochasticity and live-key-only verification. |
| Per-field Gemini call with a reference crop | Rejected. About 4× the detection cost, still stochastic, and one prototype run failed without a clear diagnosis. |
| Gemini Flash at temperature 0 | Rejected. Same information diet, same label-vs-display confusion: model roulette, not a fix. |
| Hybrid: aligner first, Gemini as fallback | Rejected. It reintroduces the known-bad detector exactly where it is least anchored. The 0.55 reference fallback degrades more honestly. |
| **Deterministic reference-box transfer (homography + NCC)** | **Chosen.** Uses reference pixels, costs $0, is reproducible and is measurable offline. |

## Decision

Remove Gemini from Stage B. Each target is aligned to the reference with classical computer vision, and every `reference_box` is carried across:

1. EXIF-upright both images, convert to grayscale, and resize to a 1024 px working edge. Reference features are computed once per job.
2. AKAZE features, brute-force Hamming 2-NN with Lowe's ratio test (0.75), at least 8 matches.
3. RANSAC homography H (3 px threshold, 2000 iterations, confidence 0.995), then sanity gates.
4. Per field: project the box through H, or rescue it with NCC, or fall back to `reference_box` at 0.55. Otherwise write a failed cell.

The Stage B output contract (`detected_box`, `box_confidence`), the combined-confidence formula (0.4 × box + 0.6 × value), Stage C and the review table are unchanged. Stage A still runs: `expected_format` stays load-bearing, but Stage B no longer reads `semantic_description`. Boxes, crops and overlays are canonicalized to EXIF-upright space across the worker and the web upload-confirm route.

### Engineering rules (binding)

- **A5: Seeded, legacy RANSAC.** `cv2.setRNGSeed` is called *inside* each synchronous aligner entry point. OpenCV's RNG is per thread and the `asyncio.to_thread` pool reuses threads, so import-time seeding is not enough. Only legacy `cv2.RANSAC` is used, never USAC, which is documented as nondeterministic.
- **A6: Pinned OpenCV.** OpenCV is pinned to an exact version in `pyproject.toml`. All co-installed `cv2` wheels are pinned to the same version, and the Docker image reinstalls the headless build last. `cells.model_versions.detector` records `align-v<N>+opencv<cv2.__version__>` (e.g. `align-v1+opencv4.13.0`). Bit-identical results are claimed within one OpenCV version only.
- **A7: Reference-side feature mask and H sanity gates.** Reference keypoints are kept only inside the union of the reference boxes, each dilated 2.5× about its center, so a background plane (wall, whiteboard) cannot win RANSAC. H is rejected unless the projected region is convex and not mirrored, scale is 0.5–2.0, rotation is ≤ 30°, and displacement is ≤ 25% of the target diagonal.
- **A8: Bezel-padded, translation-only NCC with gates.** The template is the reference box padded by 50% of its width/height on each side, so the static bezel ring dominates over changing digits. The search window is the box ± 100% of its dimensions. A match passes only when peak ≥ 0.50 AND shift ≤ 0.3 × min(box w, h). With a *strong* H (≥ 30 inliers AND inlier ratio ≥ 0.4), NCC only verifies: a fail keeps the H box, because glare or a digit change is not misalignment. With a weak or rejected H, NCC around the raw reference location is the rescue rung.
- **A9: Confidence bands keyed to H strength, not NCC score.** Strong H + NCC confirm 0.93 · strong H alone 0.85 · NCC rescue 0.70 · reference-box fallback 0.55. The fallback can never reach `ok`: the best combined score is 0.4 × 0.55 + 0.6 × 1.0 = 0.82 < 0.85.
- **A10: Pure module.** `app/pipeline/align.py` does not import `app.settings`, and per-image alignment latency is logged. Thresholds may only be retuned on evidence from the offline eval harness.

## Consequences

**Positive**

- **Deterministic:** the same image and reference always give the same H and boxes (within one OpenCV version).
- **$0 detection:** a 50-image × 4-field job drops from ~$0.40 to ~$0.08 (about an 80% cut). Stage B goes from 30–60 s of model calls per batch to ~35–45 ms CPU per image.
- **Offline-verifiable:** `apps/worker/eval/run_eval.py` scores any backend against hand-labeled ground truth with no network access. The pre-Phase-7 detector is preserved as a frozen, eval-only baseline, so before/after numbers stay reproducible.
- **Immune to model and prompt drift.** The old detection prompt, schema and retry path are removed from `app`.

**Negative / limits**

- **Assumes near-identical batches.** Large viewpoint changes, occlusion or featureless instrument faces degrade to the 0.55 fallback, which lands in `low_confidence` / `needs_review` and is never silently `ok`.
- **Parallax.** One homography models one plane. When the camera translates, fields on instruments at different depths move differently, so a single H can leave a box offset on a field off the dominant plane. Stage C is the backstop: an offset crop typically yields an illegible read or a Flash/PaddleOCR disagreement, both of which force `needs_review`. The crop's +8% padding absorbs small residuals.
- **OpenCV upgrades are deliberate events.** Bump all `cv2` wheels together, bump `align-vN` if behavior changes, and re-run the eval.

## Results

Offline eval on the committed fixture set: 12 targets (aligned, shifted, EXIF-6 and lighting classes), 48 field instances, hit = IoU ≥ 0.5.

| Stage B detector | Mean IoU | Hit-rate@0.5 | Hit-rate, shifted class (n = 24) | Cost / determinism |
|---|---|---|---|---|
| Gemini 2.5 Pro (pre-Phase-7) | 0.389 | 38% | 8% | 1 Pro call per image, stochastic |
| `reference_box` passthrough | 0.467 | 44% | 4% | $0, deterministic |
| **Deterministic aligner** | **0.830** | **96%** | **92%** | $0, bit-identical across runs |

The aligner also scores 100% on the aligned, EXIF-6 and lighting classes.

## Rollback

Phase 7 landed as independent slices: eval harness, EXIF upright-space fix, aligner, cleanup. Reverting the aligner and cleanup changes restores the Gemini detection path exactly. The EXIF fix and the eval harness are independent improvements and stay. No database schema changes were made: `detected_box` and `box_confidence` keep their shape, and `model_versions` is free-form JSONB, so rows written by either detector coexist and stay attributable. The frozen legacy detector lets the harness re-measure Gemini at any time (`run_eval.py --backend gemini`, needs an API key).

## Revisit triggers

- More than ~30% of production fields on the 0.55 fallback (`stage_b.detector_raw` telemetry): recalibrate thresholds on eval evidence.
- The aligner scores below the `reference_box` baseline on any fixture class: fix NCC windowing or thresholds before considering model detectors.
- Batches routinely violate the near-identical premise: that is a product change. A per-field model detector with reference crops becomes the candidate, judged on the same harness.
