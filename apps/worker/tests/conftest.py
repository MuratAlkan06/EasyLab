import pytest

from app.pipeline import paddle_ocr


@pytest.fixture(autouse=True)
def _reset_paddle_ocr_singleton() -> None:
    """PaddleOCR caches a process-global instance + availability flag. Reset
    both before every test so monkeypatched fakes from one test don't leak
    into the next."""
    paddle_ocr.reset_for_tests()
