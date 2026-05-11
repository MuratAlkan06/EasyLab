"""Unit tests for confidence scoring and cell-status derivation."""

from __future__ import annotations

from app.pipeline.confidence import compute_cell_status, compute_combined_confidence


def test_combined_confidence_formula() -> None:
    assert compute_combined_confidence(0.8, 0.9) == 0.86


def test_combined_confidence_rounding() -> None:
    # 0.4*0.123456 + 0.6*0.987654 = 0.642174  -> 0.642 (3dp)
    result = compute_combined_confidence(0.123456, 0.987654)
    # Result should have <= 3 decimal places: scaled by 1000 it must be an integer.
    assert abs(result * 1000 - round(result * 1000)) < 1e-9
    assert result == 0.642


def test_status_ok() -> None:
    assert (
        compute_cell_status(
            combined_confidence=0.90,
            legible=True,
            validation_error=None,
            found=True,
            parsed_value_is_null=False,
        )
        == "ok"
    )


def test_status_low_confidence() -> None:
    assert (
        compute_cell_status(
            combined_confidence=0.70,
            legible=True,
            validation_error=None,
            found=True,
            parsed_value_is_null=False,
        )
        == "low_confidence"
    )


def test_status_needs_review_low_conf() -> None:
    assert (
        compute_cell_status(
            combined_confidence=0.50,
            legible=True,
            validation_error=None,
            found=True,
            parsed_value_is_null=False,
        )
        == "needs_review"
    )


def test_status_needs_review_not_legible() -> None:
    assert (
        compute_cell_status(
            combined_confidence=0.90,
            legible=False,
            validation_error=None,
            found=True,
            parsed_value_is_null=False,
        )
        == "needs_review"
    )


def test_status_needs_review_validation_error() -> None:
    assert (
        compute_cell_status(
            combined_confidence=0.92,
            legible=True,
            validation_error="unit mismatch",
            found=True,
            parsed_value_is_null=False,
        )
        == "needs_review"
    )


def test_status_failed_not_found() -> None:
    assert (
        compute_cell_status(
            combined_confidence=0.95,
            legible=True,
            validation_error=None,
            found=False,
            parsed_value_is_null=False,
        )
        == "failed"
    )


def test_status_failed_null_value() -> None:
    assert (
        compute_cell_status(
            combined_confidence=0.95,
            legible=True,
            validation_error=None,
            found=True,
            parsed_value_is_null=True,
        )
        == "failed"
    )


def test_failed_beats_needs_review() -> None:
    # All "needs_review" triggers are also active, but `failed` precedence wins.
    assert (
        compute_cell_status(
            combined_confidence=0.20,
            legible=False,
            validation_error="bad",
            found=False,
            parsed_value_is_null=False,
        )
        == "failed"
    )
