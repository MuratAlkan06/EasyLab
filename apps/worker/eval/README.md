# Stage B detection eval harness

Offline measurement rig for Stage B (region detection), built first in Phase 7
(ADR-007, Slice 1) so the Gemini-detector replacement is judged on numbers, not
on eyeballing a job. It scores a detector backend against hand-labeled ground
truth and reports mean IoU and hit-rate@threshold per field, per fixture class,
in aggregate, and as the worst case per image.

The harness imports pipeline modules (`app.pipeline.*`). Nothing in `app`
imports the harness, and pytest never collects it (`testpaths = ["tests"]`, and
there are no `test_*.py` files here).

## Run it

From `apps/worker` (so `pydantic-settings` reads `apps/worker/.env`):

```bash
.venv/bin/python eval/run_eval.py --backend reference     # offline baseline, no network
.venv/bin/python eval/run_eval.py --backend gemini        # today's production Stage B (paid)
.venv/bin/python eval/run_eval.py --backend reference --iou-threshold 0.7
```

| backend     | what it predicts                                                                 | network |
|-------------|----------------------------------------------------------------------------------|---------|
| `reference` | the reference image's GT box for each field (= Stage B's `reference_box` fallback) | no      |
| `align`     | homography reference-box transfer: **lands in Slice 3** (stub exits)             | no      |
| `gemini`    | the real production call: `preprocess_for_stage_b` → `stage_b._call_with_retry` → `call_stage_b` (same prompt, model, retry) → the same acceptance ladder (confidence ≥ 0.30, `is_box_valid`, 0.55 reference fallback) | yes, ~1 Pro call per target |

`gemini` skips with a message when `GEMINI_API_KEY` is unset or
`google-genai` is not importable. It sends each field's
`semantic_description` from `ground_truth.json`. These are hand-written to
mirror what Stage A produces, and the `[ref position]` hints come from the
reference image's GT boxes.

`run_eval.py` sets placeholder `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` /
`DATABASE_URL` / `INTERNAL_SHARED_SECRET` before importing `app.*`
(`Settings()` needs them at import time). Those placeholders also mean the
harness can never reach the real Supabase or Postgres.

Outputs (gitignored) go to `eval/out/<backend>/<fixture-set>/`:
`metrics.json` holds every number plus per-pair boxes and sources, and
`overlays/<image>.jpg` shows the GT box in **green** and the prediction in
**blue** (label = field, IoU, source).

## Scoring space and EXIF (read before trusting the exif6 row)

Ground truth is defined in **upright** space, with EXIF orientation applied,
because that's how users see and annotate images. A prediction is the
normalized box Stage B hands to Stage C, and Stage C crops it from whatever
canvas `stage_c._open_image` returns. The harness asks Stage C for that canvas
and maps the prediction into upright space before computing IoU. Today Stage C
opens **raw** bytes, so on the `exif6` fixture every backend scores ~0. Once
Slice 2 moves Stage C to upright space, the same run jumps to the score of the
fixture it was derived from (`target_02`), with no harness change. If Slice 2
renames `_open_image`, update `_stage_c_canvas()` in `run_eval.py`.

**Overlay JPEGs are drawn in RAW orientation until Slice 2 lands.** They go
through the pipeline's own `render_overlay`, which doesn't apply EXIF yet. They
are only wrong for the `exif6` fixture: there the GT box is misplaced, while
the prediction box shows where Stage C would really crop.

## Fixtures (`eval/fixtures/`)

There are 13 JPEGs from one real lab-bench batch: a black bench DC power supply
(two LCDs above printed VOLT / AMP labels), an Extech EX410A handheld meter, and
an HP 3468A bench multimeter.

| file            | class      | how it differs from `reference.jpg`                                         |
|-----------------|------------|------------------------------------------------------------------------------|
| `reference.jpg` | reference  | the image the "user" annotated (its GT boxes = the template's reference boxes) |
| `target_01..04` | `aligned`  | same framing; instruments displaced ≤ ~2% of the diagonal, no visible roll/zoom |
| `target_05..10` | `shifted`  | real camera moves: 3-5% shifts, 1-7.5° roll, 4-18% zoom-out; `target_09` zoomed in |
| `target_11`     | `exif6`    | `target_02`'s pixels stored rotated 90° CCW with EXIF Orientation=6, so EXIF-aware viewers show `target_02` |
| `target_12`     | `lighting` | `target_01` darkened, warm cast, low contrast, plus specular glare over the PSU-volt, HP and Extech displays |

`exif6` and `lighting` are pixel-preserving derivations, so their GT equals
their source's GT (`gt_io.py` enforces this). Comparing each row with its
source isolates the orientation or photometric effect.

### Privacy (A1) — these are bench-crop metrics

The originals show classmates and a classroom. **No person appears in any
committed fixture.** Each fixture is:

1. cropped to one fixed bench rectangle shared by every frame, so real camera
   motion between frames is preserved;
2. **flat-grey redacted** outside the instruments' silhouette. Bystanders stood
   at display height directly behind every instrument, so no rectangle could
   exclude them on its own. The grey covers everything above the
   PSU/HP top edges, around the Extech outline, and the hand holding the
   Extech in some frames;
3. re-encoded from raw pixels: no EXIF/ICC/XMP/comments (only `target_11`
   carries an EXIF block, holding just Orientation=6). Names are neutral; ≤
   1600 px; ~2.1 MB total.

So **committed metrics are bench-crop metrics**. The classroom background is
gone, and with it the "background plane wins RANSAC" failure (ADR-007 A7).
Exercise that failure with private full-frame fixtures (below).

Rebuild from originals with `make_fixtures.py --spec <private spec>`. The spec
holds source paths plus keep/redact polygons and lives in the gitignored
`eval/fixtures_private/`. The script itself contains no paths, dates or
locations, and verifies every output's metadata after writing.

## Ground truth format (`fixtures/ground_truth.json`)

```jsonc
{
  "format_version": 1,
  "reference": "reference.jpg",
  "fields": [{"name": "psu_volt_display", "semantic_description": "..."}],
  "images": [
    {"file": "target_11.jpg", "class": "exif6", "derived_from": "target_02.jpg",
     "note": "...", "boxes": {"psu_volt_display": [x, y, w, h], "handheld_display": null}}
  ]
}
```

* `[x, y, w, h]` are normalized 0..1 against the **upright** image, origin top-left.
* `class` ∈ `reference | aligned | shifted | exif6 | lighting`; exactly one `reference`.
* **Absent convention:** `null` means the field's live display isn't visible or
  legible in that image, and it is excluded from scoring. A field that is
  `null` in the reference is not in the template, because a user couldn't have
  annotated it. (All committed images have all four fields.)
* **GT convention:** a tight box on the *live readout window*, i.e. the display
  glass that holds the changing characters. For the PSU that's each light LCD
  panel. For the Extech it's the LCD window including its thin dark-grey
  frame. For the HP 3468A it's the tinted readout strip, value plus `ADC`
  annunciator. When the window is tilted (perspective/roll) the box is its
  axis-aligned bounding box. It is **never** the printed VOLT/AMP labels,
  nameplates or bezels; those labels are exactly what today's detector boxes.

Fields: `psu_volt_display`, `psu_amp_display`, `handheld_display` (Extech
LCD), `bench_meter_display` (HP 3468A readout).

### Labeling

`label_gt.py` renders each image's GT through the pipeline's `render_overlay`
in upright space, plus 3× zooms per box with a pixel ruler, into
`eval/out/labels/`. Edit `ground_truth.json`, re-run, and repeat until every
box hugs its window (labels here are within ~2-3 px).

```bash
.venv/bin/python eval/label_gt.py                    # all images + zooms
.venv/bin/python eval/label_gt.py --image target_05.jpg
```

## Private full-frame fixtures

`eval/fixtures_private/` is gitignored, so put un-redacted originals there. Add
`eval/fixtures_private/ground_truth.json` in the same format (boxes normalized
to the full frames, `reference` + at least one target). `run_eval.py` then
evaluates it automatically after the committed set and writes to
`eval/out/<backend>/fixtures_private/`. Point at any other set with
`--gt path/to/ground_truth.json` (repeatable). Never commit anything from that
directory.
