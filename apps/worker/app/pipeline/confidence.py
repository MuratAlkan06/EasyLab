"""Confidence scoring and cell status derivation.

Pure functions. No I/O. Used by the worker after Stage B/C produces a
``box_confidence`` (from detection) and a ``value_confidence`` (from value
extraction) for each (image, field) pair.
"""

from __future__ import annotations

# Weight split between detection confidence and value-extraction confidence.
# Value extraction is weighted higher because a correctly extracted value is
# the user-facing artifact; a tight box with the wrong value is still wrong.
_BOX_WEIGHT = 0.4
_VALUE_WEIGHT = 0.6

# Status thresholds — see docs/ai-pipeline.md.
_NEEDS_REVIEW_THRESHOLD = 0.60
_LOW_CONFIDENCE_THRESHOLD = 0.85


def compute_combined_confidence(box_confidence: float, value_confidence: float) -> float:
    """Weighted blend of detection and value-extraction confidence, rounded to 3dp."""
    return round(_BOX_WEIGHT * box_confidence + _VALUE_WEIGHT * value_confidence, 3)


def compute_cell_status(
    *,
    combined_confidence: float,
    legible: bool,
    validation_error: str | None,
    found: bool,
    parsed_value_is_null: bool,
) -> str:
    """Derive the discrete cell status from extraction signals.

    Precedence (first match wins):
      1. ``failed``         — nothing usable was produced.
      2. ``needs_review``   — produced but flagged for human attention.
      3. ``low_confidence`` — produced, no flags, but below the high-confidence bar.
      4. ``ok``             — high confidence, legible, valid.
    """
    if not found or parsed_value_is_null:
        return "failed"
    if combined_confidence < _NEEDS_REVIEW_THRESHOLD or not legible or validation_error is not None:
        return "needs_review"
    if combined_confidence < _LOW_CONFIDENCE_THRESHOLD:
        return "low_confidence"
    return "ok"
