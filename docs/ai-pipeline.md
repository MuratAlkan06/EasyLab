# EasyLab — AI Pipeline Specification (v1 MVP)

> Generated: 2026-05-05
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
   Gemini 2.5 Pro
   Input:  image + all template fields with semantic_description
   Output: detected_box + box_confidence per field (or found=false)
          │
          ├─── found=false → cell.status='failed', skip Stage C
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

**Key rule:** EXIF transpose is applied to every image before any stage.

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

---

## 4. Stage B — Region Detection {#stage-b}

**Model:** Gemini 2.5 Pro
**When:** Once per non-reference image, run in parallel (IMAGE_CONCURRENCY=5)

### Preprocessing

1. EXIF transpose
2. Resize: longest edge → 1568 px, preserve aspect ratio
3. Encode: JPEG quality = 88

### GenerationConfig

```python
generation_config = GenerationConfig(
    temperature=1.0,
    thinking_config=ThinkingConfig(thinking_budget=-1),  # spatial reasoning benefits
    max_output_tokens=1024,
    response_mime_type="application/json",
    response_schema=DetectionResponse.model_json_schema(),
)
```

### Pydantic Response Schema

```python
class DetectedField(BaseModel):
    field_name: str
    found: bool
    box: list[int] | None  # [ymin, xmin, ymax, xmax] in 0..1000 space (Gemini native)
    box_confidence: float  # 0.0..1.0

class DetectionResponse(BaseModel):
    fields: list[DetectedField]
```

### Box Coordinate Conversion

Gemini returns `[ymin, xmin, ymax, xmax]` in 0..1000 space. Convert to normalized 0..1:

```python
x = xmin / 1000
y = ymin / 1000
width  = (xmax - xmin) / 1000
height = (ymax - ymin) / 1000
```

Store as `{"x": float, "y": float, "width": float, "height": float}` in `cells.detected_box`.

### Box Rejection Criteria

After coordinate conversion, reject detected box (treat as found=false) if:
- Box area in original image < 25 px²
- Box width or height in original image < 8 px
- Box covers > 60% of the total image area (likely a hallucination)

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

1. Crop from **original-resolution** image (not the 1568px detection version)
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
| failed | found = false OR parsed_value is null after retry |

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
| JSON schema validation failure (Stage A/B/C) | Retry once; if second failure: see stage-specific fallback |
| found = false (Stage B) | Skip Stage C, write failed cell with `failure_reason = 'field_not_visible'` |
| Box rejected (area/dimension/coverage check) | Treat as found=false |
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
- 180s chosen because Stage B (detection) can take 30–60s per batch; 60s threshold was too aggressive

### max_output_tokens per Stage

| Stage | Model | max_output_tokens |
|---|---|---|
| A — Template Gen | Gemini 2.5 Pro | 2048 |
| B — Detection | Gemini 2.5 Pro | 1024 |
| C — Extraction | Gemini 2.5 Flash | 256 |

### Cost Estimate (50 images × 4 fields)

| Stage | Calls | Est. Cost |
|---|---|---|
| A — Template Gen | 1 | ~$0.01 |
| B — Detection | 50 | ~$0.32 |
| C — Extraction | 200 | ~$0.07 |
| **Total** | **251** | **~$0.40–0.46** |

Estimated latency: **~50 seconds** end-to-end with IMAGE_CONCURRENCY=5.

### Spend Controls

Checked by Next.js at job enqueue (`POST /api/projects/:id/jobs`). Returns 429 if exceeded.

| Env var | Default | Description |
|---|---|---|
| MAX_JOBS_PER_WORKSPACE_PER_DAY | 5 | Per workspace, resets at midnight |
| MAX_IMAGES_PER_WORKSPACE_PER_DAY | 200 | Per workspace |
| GLOBAL_DAILY_TOKEN_BUDGET | 50,000,000 | Across all workspaces combined |

Worker also checks global token budget before Stage A. If global budget is exhausted mid-job, job is paused (not failed) and retried next day.
