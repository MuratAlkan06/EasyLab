# EasyLab — AI Pipeline Specification (v1 MVP)

> Generated: 2026-05-05 · Updated: 2026-10-18 (Phase 7: Stage B detection is now a deterministic aligner, no model call. See ADR-007)
> Owner: FastAPI worker on Fly.io. Next.js never calls Gemini directly.

---

## Table of Contents

1. [Pipeline Overview](#overview)
2. [Image Validation (at upload)](#image-validation)
3. [Stage A — Template Generation](#stage-a)
4. [Stage B — Region Detection](#stage-b)
5. [Stage C — Value Extraction](#stage-c)
6. [Confidence → Cell Status](#confidence)
7. [PaddleOCR Reconciliation](#ocr)
8. [Error Handling](#errors)
9. [Circuit Breaker & 429 Handling](#circuit-breaker)
10. [Concurrency & Cost](#concurrency)

---

## 1. Pipeline Overview {#overview}

```
Reference image + field annotations
          │
          ▼
   Stage A — Template Generation (once per project)
   Gemini 2.5 Pro
   Input:  reference image + field list with reference_box
   Output: semantic_description + expected_format per field
          │
          ▼ (stored in template_fields)
          │
For each non-reference image in the batch:
          │
          ▼
   Stage B — Region Detection (once per image)
   Deterministic aligner (OpenCV on CPU, no model call)
   Input:  image + reference image + reference_box per field
   Output: detected_box + box_confidence per field
           (not located → reference_box fallback at 0.55)
          │
          ├─── no usable box → cell.status='failed', skip Stage C
          │
          ▼
   Stage C — Value Extraction (once per detected region per field)
   Gemini 2.5 Flash  +  PaddleOCR (parallel)
   Input:  cropped region at original resolution
   Output: raw_text, parsed_value, unit_seen, legible, self_confidence
          │
          ▼
   Reconcile Flash + PaddleOCR results
   Write cell row with combined_confidence + status
```

**Key rule:** EXIF transpose is applied to every image before any stage. Boxes, crops and overlays all live in that upright space (the space the user annotated in).

---

## 2. Image Validation (at upload) {#image-validation}

Checked by Next.js before the signed upload URL is issued. FastAPI also validates on first read.

| Rule | Action on failure |
|---|---|
| File size > 20 MB | Reject with 400: "File too large. Maximum is 20 MB." |
| MIME type is HEIC/HEIF | Reject with 400: "Convert to JPG or PNG before uploading." |
| Magic bytes don't match JPEG or PNG | Reject with 400: "Unrecognized file format." |
| Width or height > 12,000 px | Reject with 400: "Image dimensions too large." |

---

## 3. Stage A — Template Generation {#stage-a}

**Model:** Gemini 2.5 Pro
**When:** Once per project, triggered after user saves annotations and clicks "Start Processing"

### Preprocessing

1. EXIF transpose (rotate to upright)
2. Resize: longest edge → 1568 px (Gemini's optimal input resolution), preserve aspect ratio
3. Encode: JPEG quality = 90

### GenerationConfig

```python
generation_config = GenerationConfig(
    temperature=1.0,           # required when thinking is enabled
    thinking_config=ThinkingConfig(thinking_budget=-1),  # dynamic thinking
    max_output_tokens=2048,
    response_mime_type="application/json",
    response_schema=TemplateGenerationResponse.model_json_schema(),
)
```

### Pydantic Response Schema

```python
class FieldTemplate(BaseModel):
    field_name: str
    semantic_description: str   # "7-segment LCD display showing voltage reading in the top-left of the meter"
    expected_format: dict | None  # {"type": "number", "unit": "V"} or null

class TemplateGenerationResponse(BaseModel):
    fields: list[FieldTemplate]
```

### Retry & Fallback

- On JSON schema validation failure: retry once with same inputs
- On second failure: set `projects.template_quality = 'degraded'`, synthesize semantic_description from field_name + reference_box position (e.g. "field named 'Voltage' at top-left of image")
- User sees a warning banner before processing: "Template quality degraded — accuracy may be lower"

Since Phase 7, Stage B does not read `semantic_description` (it locates fields geometrically, see §4). It is still generated and stored. `expected_format` remains load-bearing: Stage C uses it for hints and validation.

---

## 4. Stage B — Region Detection {#stage-b}

**Method:** Deterministic reference-box transfer (`app/pipeline/align.py`): feature-based homography plus template-matching verification, OpenCV on CPU. **No model call, no token cost.**
**When:** Once per non-reference image, run in parallel (IMAGE_CONCURRENCY=5) via `asyncio.to_thread`. The reference image is downloaded once per job and its features are precomputed. Only the 1024 px grayscale and the keypoints/descriptors are kept.

Batches are near-identical photos of the same setup, so the user's own `reference_box` is the strongest signal available. Stage B estimates how the camera moved between the reference and each image, then carries every box across.

For why Gemini detection was replaced, the options rejected, and the eval evidence, see [ADR-007](adr-007-stage-b-detection.md).

### Preprocessing

1. EXIF transpose (reference and target alike, so both are upright)
2. Grayscale
3. Resize: longest edge → 1024 px working resolution (up- or downscaled)

### Alignment

1. **Features:** AKAZE. Reference-side keypoints are kept only inside the union of the reference boxes, each dilated 2.5× about its center. A static background plane (whiteboard, wall) therefore cannot win RANSAC.
2. **Matching:** brute-force Hamming 2-NN with Lowe's ratio test (0.75). At least 8 matches are required to estimate H.
3. **Homography:** legacy `cv2.RANSAC` (never USAC): 3 px reprojection threshold, 2000 iterations, confidence 0.995. `cv2.setRNGSeed` is called inside each synchronous entry point, because OpenCV's RNG is per thread and the `to_thread` pool reuses threads. The same image and reference always give the same H.
4. **Sanity gates** on the projected region of interest (the bounds of the feature mask). H is rejected unless all of these hold:
   - the projected quad is convex and not mirrored
   - scale (√ area ratio) is 0.5–2.0
   - rotation is ≤ 30°
   - center displacement is ≤ 25% of the target diagonal
5. **Strength:** H is *strong* with ≥ 30 RANSAC inliers AND inlier ratio ≥ 0.4. Otherwise it is *weak*.

### Per-field box

| H | Box | NCC role |
|---|---|---|
| strong | axis-aligned bounds of the reference box projected through H (`perspectiveTransform`) | **verification only**: a pass raises confidence; a fail keeps H's box and band. Glare or a digit change is not misalignment, so NCC never moves a strong-H box |
| weak / rejected / none | reference box moved by the NCC shift | **rescue**: NCC around the raw reference location (identity transfer) |
| nothing passes | reference box unchanged | fallback |

NCC is `cv2.matchTemplate(TM_CCOEFF_NORMED)`, translation only, run in the reference frame (for strong H, the target is first warped into it). The template is the reference box padded by 50% of its width/height on each side. The bezel ring is then ~75% of the template, so changing digits don't dominate. The search window is the box ± 100% of its dimensions. A match **passes** when peak ≥ 0.50 AND shift ≤ 0.3 × min(box width, box height).

### Confidence bands

Bands are keyed to H strength, not to the NCC score. `method` is the value logged in `stage_b.detector_raw`.

| Outcome | `method` | box_confidence |
|---|---|---|
| strong H + NCC confirmed | `homography+ncc` | 0.93 |
| strong H, NCC not confirmed | `homography` | 0.85 |
| weak/no H + NCC rescue | `ncc_rescue` | 0.70 |
| reference-box fallback | `fallback` | 0.55 |

The 0.55 fallback can never reach `ok`: the best possible combined score is 0.4 × 0.55 + 0.6 × 1.0 = 0.82 < 0.85 (§6). This is intentional. A strong-H box with a clean Stage C read (value_confidence ≥ 0.85) can reach `ok`.

### Acceptance ladder and fallback

Unchanged from the Gemini-era ladder in `stage_b.py`:

1. A box with box_confidence < 0.30 is rejected (`failure_reason = 'low_confidence'`).
2. A box failing the rejection criteria below is rejected.
3. Any rejected or unlocated field falls back to its `reference_box` at **0.55**, provided that box is itself valid on this image. Otherwise a failed cell is written (`failure_reason = 'field_not_visible'` unless low confidence) and Stage C is skipped for that field.
4. If alignment raises, the same per-field fallback applies. If the image can't be downloaded or decoded, every field becomes a failed cell and the image task ends `not_found`.

### Box Rejection Criteria

Reject a box (treat as not located) if:
- Box area in original image < 25 px²
- Box width or height in original image < 8 px
- Box covers > 60% of the total image area

### Output

Boxes are stored in `cells.detected_box` as `{"x": float, "y": float, "width": float, "height": float}`, normalized 0..1 against the upright original image. `cells.model_versions.detector` is `align-v<N>+opencv<cv2.__version__>` (e.g. `align-v1+opencv4.13.0`). Results are bit-identical across runs within one OpenCV version, so the worker pins OpenCV exactly in `pyproject.toml`. Bump `align-vN` whenever the algorithm or thresholds change.

### Telemetry

Logged for every field regardless of `STAGE_B_DEBUG`: `stage_b.detector_raw` with method, box_confidence, detector box, NCC peak, and IoU vs `reference_box`. Every image logs `stage_b.image.aligned` with align_ms, h_status, matches and inliers. More than ~30% of fields on the 0.55 fallback in production is the trigger to recalibrate thresholds. `STAGE_B_DEBUG=true` also uploads per-image overlay JPEGs.

### Accuracy and limits

Offline eval (`apps/worker/eval/run_eval.py`, 12 bench-crop targets, 48 field instances): **mean IoU 0.830, hit-rate@0.5 96%**. The pre-Phase-7 Gemini detector scored 0.389 / 38% and plain reference-box passthrough 0.467 / 44%. Alignment takes ~35–45 ms per image on the dev machine. Thresholds may only be retuned on eval evidence.

The aligner assumes near-identical batches. Large viewpoint changes, occlusion, or featureless instrument faces degrade to the 0.55 fallback (→ `low_confidence` / `needs_review`). Stage C legibility and Flash/OCR disagreement are the second safety net.

---

## 5. Stage C — Value Extraction {#stage-c}

**Model:** Gemini 2.5 Flash + PaddleOCR (parallel)
**When:** Once per detected region per field, after Stage B succeeds

### Crop Generation

1. Take `detected_box` (normalized 0..1) × original image dimensions → pixel crop rect
2. Add +8% padding on each side (clamped to image bounds)
3. Save crop to Supabase Storage: `projects/{project_id}/crops/{image_id}/{field_id}.jpg`
4. Store path in `cells.crop_path`

### Preprocessing for Gemini Flash

1. Crop from the **original-resolution** upright image (not a downscaled copy)
2. Resize: longest edge → 768 px maximum
3. Encode: JPEG quality = 92

### Gemini Flash GenerationConfig

```python
generation_config = GenerationConfig(
    temperature=0.0,           # deterministic reads
    # thinking OFF — cost control; spatial reasoning not needed for value reading
    max_output_tokens=256,
    response_mime_type="application/json",
    response_schema=ExtractionResponse.model_json_schema(),
)
```

### Pydantic Response Schema

```python
class ExtractionResponse(BaseModel):
    raw_text: str              # exactly what appears in the image
    parsed_value: float | int | str | bool | None
    unit_seen: str | None      # unit string as it appears (e.g. "V", "mA")
    legible: bool              # false if text is too blurry/obscured to read
    self_confidence: float     # model's own confidence estimate 0.0..1.0
```

### Validation Against expected_format

After extraction, validate:
- If `expected_format.type == "number"`: parsed_value must be numeric, else `validation_error = "expected number, got string"`
- If `expected_format.unit` is set: unit_seen must match (case-insensitive), else `validation_error = "unit mismatch: expected V, got mA"`
- Validation failures set `cells.validation_error` and force `cells.status = 'needs_review'`

---

## 6. Confidence → Cell Status {#confidence}

```
combined_confidence = round(0.4 × box_confidence + 0.6 × value_confidence, 3)
```

`value_confidence` is the average of Gemini Flash `self_confidence` and PaddleOCR confidence (see §7 for reconciliation).

| cells.status | Condition |
|---|---|
| ok | combined_confidence ≥ 0.85 AND legible = true AND validation_error IS NULL |
| low_confidence | combined_confidence in [0.60, 0.85) |
| needs_review | combined_confidence < 0.60 OR legible = false OR validation_error IS NOT NULL |
| failed | no usable box from Stage B (not located and no valid reference_box) OR parsed_value is null after retry |

`failed` cells are always written so the review table has a complete row for every (image × field) pair.

---

## 7. PaddleOCR Reconciliation {#ocr}

PaddleOCR runs in parallel with Gemini Flash on the same crop. Both results are available before the cell is written.

### Reconciliation Rules

| Scenario | value_confidence | Action |
|---|---|---|
| Both readers agree (parsed values match after normalization) | `max(flash_confidence, ocr_confidence)` | Use Gemini Flash value |
| Readers disagree | `min(flash_confidence, ocr_confidence) − 0.2` | Set `legible = false`, `status = needs_review` |
| PaddleOCR fails / returns empty | Flash confidence only | Use Gemini Flash value as-is |
| Gemini Flash `legible = false` but OCR succeeds | OCR confidence | Use OCR value, set `legible = true` |

### PaddleOCR Setup

- pip package: `paddlepaddle` + `paddleocr` (~500 MB first download)
- No custom preprocessing — feed the same 768px JPEG crop
- Run at 3× zoom if crop is < 48px tall (improves accuracy on small displays)

---

## 8. Error Handling {#errors}

| Failure mode | Handling |
|---|---|
| JSON schema validation failure (Stage A/C) | Retry once; if second failure: see stage-specific fallback |
| Field not located (Stage B) | Use `reference_box` at 0.55 if valid; otherwise skip Stage C, write failed cell with `failure_reason = 'field_not_visible'` |
| Box rejected (area/dimension/coverage check) | Treat as not located |
| Alignment error (Stage B) | Per-field `reference_box` fallback, as above |
| legible = false (Stage C) | Write cell with `needs_review`, include PaddleOCR fallback if available |
| parsed_value null after retry | Write failed cell with `failure_reason = 'extraction_failed'` |
| validation_error | Write cell with `needs_review` and `validation_error` text |
| Gemini 429 | Honor `retry-after` header; set `jobs.paused_until`; pause all in-flight calls |
| Gemini 5xx (≥5 consecutive) | Circuit breaker: set `jobs.paused_until = now() + 60s` |
| asyncpg connection error | Retry with exponential backoff (1s, 2s, 4s); log and fail job if all retries exhausted |

Retry backoff for API errors: 1s → 2s → 4s (tenacity `wait_exponential`).

---

## 9. Circuit Breaker & 429 Handling {#circuit-breaker}

### Circuit Breaker

- Tracks consecutive Gemini 5xx responses in worker process memory (per-job counter)
- Threshold: **5 consecutive 5xx** → open the circuit
- Action: set `jobs.paused_until = now() + interval '60 seconds'`
- Worker loop skips any job where `paused_until > now()`
- Counter resets on any successful Gemini response

### 429 Rate Limit Handling

- Read `retry-after` header from Gemini 429 response
- Set `jobs.paused_until = now() + retry_after_seconds`
- All in-flight Gemini calls for that job are cancelled
- Worker loop re-picks the job after `paused_until` passes

### Job Cancellation Check

The worker checks `jobs.status = 'cancelled'` between each image (after completing Stage C for one image before starting the next). If cancelled, the worker stops without failing — remaining image_tasks stay in `pending`.

---

## 10. Concurrency & Cost {#concurrency}

### Concurrency Limits

| Setting | Value | Env var |
|---|---|---|
| Jobs running simultaneously | 2 | WORKER_CONCURRENCY |
| Images detected in parallel per job | 5 | IMAGE_CONCURRENCY |
| Max concurrent Gemini calls | 10 | (product of above) |

### Worker Heartbeat

- Heartbeat written every **5 seconds** (`jobs.heartbeat_at`)
- Stale threshold: **180 seconds** — jobs not heartbeated for 3 minutes are reclaimed
- 180s was chosen when Stage B made Gemini calls (30–60s per batch) and a 60s threshold proved too aggressive. Stage B is now sub-second CPU per image, but the threshold stays as headroom for Stage A/C model calls.

### max_output_tokens per Stage

| Stage | Model | max_output_tokens |
|---|---|---|
| A — Template Gen | Gemini 2.5 Pro | 2048 |
| B — Detection | — (no model call) | — |
| C — Extraction | Gemini 2.5 Flash | 256 |

### Cost Estimate (50 images × 4 fields)

| Stage | Calls | Est. Cost |
|---|---|---|
| A — Template Gen | 1 | ~$0.01 |
| B — Detection | 0 (CPU aligner) | $0 |
| C — Extraction | 200 | ~$0.07 |
| **Total** | **201** | **~$0.08** |

Before Phase 7, Stage B was 50 Gemini 2.5 Pro calls (~$0.32), for a ~$0.40–0.46 job.

Estimated latency: Stage B went from 30–60 s of model calls per batch to well under a second of CPU per image. End-to-end time is now dominated by the Stage A and Stage C model calls. The old **~50 seconds** end-to-end estimate (IMAGE_CONCURRENCY=5) predates Phase 7 and has not been re-measured live.

### Spend Controls

Checked by Next.js at job enqueue (`POST /api/projects/:id/jobs`). Returns 429 if exceeded.

| Env var | Default | Description |
|---|---|---|
| MAX_JOBS_PER_WORKSPACE_PER_DAY | 5 | Per workspace, resets at midnight |
| MAX_IMAGES_PER_WORKSPACE_PER_DAY | 200 | Per workspace |
| GLOBAL_DAILY_TOKEN_BUDGET | 50,000,000 | Across all workspaces combined |

Worker also checks global token budget before Stage A. If global budget is exhausted mid-job, job is paused (not failed) and retried next day.
