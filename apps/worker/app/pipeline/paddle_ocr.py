"""PaddleOCR wrapper. Loaded lazily so import cost is only paid when OCR_ENABLED=true.

Stage C uses PaddleOCR as a parallel reader to Gemini Flash for value extraction.
PaddleOCR is blocking and CPU-heavy, so:

  * The ``PaddleOCR`` class is imported inside :func:`run_ocr` (not at module
    top level) — when ``OCR_ENABLED=false`` we never pay the import cost.
  * Callers MUST invoke :func:`run_ocr` via ``asyncio.to_thread`` and gate it
    with :data:`OCR_SEMAPHORE` to cap concurrent CPU work (R4).
"""

from __future__ import annotations

import asyncio
import io
import os
import threading

import structlog
from PIL import Image

# Silence noisy paddle output before any paddle import. ``setdefault`` so
# operators can override these via the environment if they need debug logs.
os.environ.setdefault("PADDLE_DISABLE_SIGNAL_HANDLER", "1")
os.environ.setdefault("FLAGS_call_stack_level", "0")

log = structlog.get_logger()

# PaddleOCR is CPU-heavy; cap concurrent calls regardless of image_concurrency
# so a burst of crops doesn't pin every core (R4 from risk review).
OCR_SEMAPHORE = asyncio.Semaphore(2)

# Fixed confidence used when PaddleOCR returns a non-empty result. PaddleOCR's
# own per-result score doesn't translate well to our reconciliation table, so
# we treat any successful OCR read as a single high-confidence signal.
OCR_SUCCESS_CONFIDENCE = 0.85
OCR_FAIL_CONFIDENCE = 0.0

# Below this height (pixels) we upscale the crop 3× before running OCR — small
# 7-segment displays read much more reliably after a zoom (spec §7).
_SMALL_CROP_HEIGHT_PX = 48
_SMALL_CROP_ZOOM = 3

# Cached singleton — PaddleOCR uses global state (PDX) and throws on reinit.
# Lock guards against concurrent thread-pool calls racing to init the first instance.
_ocr_instance: object | None = None
_ocr_lock = threading.Lock()


def _get_ocr() -> object:
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_lock:
            if _ocr_instance is None:
                import numpy as np  # noqa: F401 — ensure numpy is imported before paddle
                from paddleocr import PaddleOCR
                _ocr_instance = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    return _ocr_instance


def run_ocr(crop_bytes: bytes) -> str | None:
    """Run PaddleOCR on a JPEG crop. Returns the top-1 text string or ``None`` on failure.

    Must be called inside ``asyncio.to_thread`` — PaddleOCR is blocking.
    Returns ``None`` if PaddleOCR raises or returns empty results. The caller
    is responsible for treating ``None`` as a failed read in the reconciliation.
    """
    try:
        import numpy as np

        img = Image.open(io.BytesIO(crop_bytes))
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Zoom small crops (e.g. small 7-segment displays) for legibility.
        if img.height < _SMALL_CROP_HEIGHT_PX:
            img = img.resize(
                (img.width * _SMALL_CROP_ZOOM, img.height * _SMALL_CROP_ZOOM),
                Image.LANCZOS,
            )

        np_array = np.array(img)
        ocr = _get_ocr()
        result = ocr.ocr(np_array, cls=True)

        if not result or not result[0]:
            return None
        # Result shape: [[ [box, (text, score)], ... ]] — take the top-1 text.
        return result[0][0][1][0]
    except Exception as exc:  # noqa: BLE001
        log.warning("paddle_ocr.run_failed", error=str(exc))
        return None


def parse_ocr_value(
    text: str | None, expected_format: dict | None
) -> float | int | str | bool | None:
    """Parse OCR text into the typed value matching ``expected_format``.

    Returns ``None`` if ``text`` is ``None``/empty or parsing fails — the caller
    treats that as "OCR did not produce a usable value" for reconciliation.
    """
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None

    fmt_type = (expected_format or {}).get("type")
    if fmt_type == "number":
        try:
            return float(stripped)
        except ValueError:
            return None
    if fmt_type == "boolean":
        lowered = stripped.lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        return None
    return stripped
