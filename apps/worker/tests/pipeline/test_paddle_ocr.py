"""Unit tests for the PaddleOCR wrapper.

PaddleOCR is mocked everywhere — we never load the real ~500MB model. The
tests assert on:
  * top-1 text extraction shape
  * graceful None on empty results / exceptions
  * typed parsing into ``expected_format``
"""

from __future__ import annotations

import io
import sys
import types
from unittest.mock import MagicMock

import pytest
from PIL import Image

from app.pipeline import paddle_ocr

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _crop_bytes(width: int = 200, height: int = 80) -> bytes:
    """Encode a tiny solid-colour JPEG so PIL can decode it inside run_ocr."""
    img = Image.new("RGB", (width, height), color="white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _install_fake_paddleocr(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ocr_return,
    raise_exc: Exception | None = None,
) -> MagicMock:
    """Inject a fake ``paddleocr`` module so ``run_ocr``'s lazy import succeeds.

    Also installs a minimal ``numpy`` shim if numpy isn't on the test path —
    we don't actually need its array semantics, only that ``np.array(img)``
    returns something the fake OCR will accept.
    """
    fake_mod = types.ModuleType("paddleocr")
    instance = MagicMock()
    if raise_exc is not None:
        instance.ocr = MagicMock(side_effect=raise_exc)
    else:
        instance.ocr = MagicMock(return_value=ocr_return)
    fake_cls = MagicMock(return_value=instance)
    fake_mod.PaddleOCR = fake_cls
    monkeypatch.setitem(sys.modules, "paddleocr", fake_mod)

    # numpy is a real transitive dep for paddle, but the test env may not have
    # it installed — only the lazy import path inside run_ocr touches numpy.
    if "numpy" not in sys.modules:
        fake_np = types.ModuleType("numpy")
        fake_np.array = lambda obj: obj  # passthrough is fine for the mock
        monkeypatch.setitem(sys.modules, "numpy", fake_np)

    return fake_cls


# ---------------------------------------------------------------------------
# run_ocr
# ---------------------------------------------------------------------------


def test_run_ocr_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-empty PaddleOCR result → top-1 text string returned."""
    # PaddleOCR result shape: [[ [box, (text, score)], ... ]].
    fake_result = [[[[[0, 0], [10, 0], [10, 5], [0, 5]], ("12.34", 0.99)]]]
    _install_fake_paddleocr(monkeypatch, ocr_return=fake_result)

    text = paddle_ocr.run_ocr(_crop_bytes())

    assert text == "12.34"


def test_run_ocr_returns_none_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty result list → None (caller treats as 'OCR did not read anything')."""
    _install_fake_paddleocr(monkeypatch, ocr_return=[])

    assert paddle_ocr.run_ocr(_crop_bytes()) is None


def test_run_ocr_returns_none_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """PaddleOCR.ocr raising must not propagate — Stage C falls back to Flash-only."""
    _install_fake_paddleocr(monkeypatch, ocr_return=None, raise_exc=RuntimeError("paddle exploded"))

    assert paddle_ocr.run_ocr(_crop_bytes()) is None


def test_run_ocr_zooms_small_crops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Crops shorter than 48px get upscaled 3× before OCR (spec §7)."""
    fake_result = [[[[[0, 0], [10, 0], [10, 5], [0, 5]], ("0.5", 0.9)]]]
    fake_cls = _install_fake_paddleocr(monkeypatch, ocr_return=fake_result)

    # 30px tall → below the 48px threshold.
    paddle_ocr.run_ocr(_crop_bytes(width=120, height=30))

    # The fake numpy.array passthrough hands the PIL image through unchanged,
    # so we can check the resized dimensions on the call argument.
    instance = fake_cls.return_value
    args, _ = instance.ocr.call_args
    handed_image = args[0]
    # 30px * 3 = 90 → height after the LANCZOS resize.
    assert handed_image.size == (360, 90)


# ---------------------------------------------------------------------------
# parse_ocr_value
# ---------------------------------------------------------------------------


def test_parse_ocr_value_number() -> None:
    """Numeric format + valid numeric text → float."""
    assert paddle_ocr.parse_ocr_value("3.14", {"type": "number"}) == 3.14


def test_parse_ocr_value_boolean_true() -> None:
    """Boolean format + truthy synonym → True."""
    assert paddle_ocr.parse_ocr_value("yes", {"type": "boolean"}) is True


def test_parse_ocr_value_text() -> None:
    """Default/text format → stripped string."""
    assert paddle_ocr.parse_ocr_value("abc", {"type": "text"}) == "abc"


def test_parse_ocr_value_none_on_bad_number() -> None:
    """Numeric format + non-numeric text → None (caller treats as OCR miss)."""
    assert paddle_ocr.parse_ocr_value("abc", {"type": "number"}) is None


def test_parse_ocr_value_none_when_text_none() -> None:
    """None text → None regardless of format."""
    assert paddle_ocr.parse_ocr_value(None, {"type": "number"}) is None
