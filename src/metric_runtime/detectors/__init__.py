"""Pluggable abnormality detectors."""

from __future__ import annotations

from typing import Any

__all__ = [
    "Detector",
    "DetectorSpec",
    "DetectorStrategy",
    "SeasonalBaselineDetector",
    "SeasonalZScore",
    "SeasonalZScoreDetector",
    "Threshold",
    "ThresholdDetector",
    "build_detector",
    "coerce_detector_spec",
]


def __getattr__(name: str) -> Any:
    # Lazy exports avoid circular imports with models ↔ policy ↔ base.
    if name in {
        "Detector",
        "DetectorStrategy",
        "SeasonalBaselineDetector",
        "SeasonalZScoreDetector",
        "ThresholdDetector",
    }:
        from metric_runtime.detectors import base as _base

        return getattr(_base, name)
    if name in {
        "DetectorSpec",
        "SeasonalZScore",
        "Threshold",
        "build_detector",
        "coerce_detector_spec",
    }:
        from metric_runtime.detectors import policy as _policy

        return getattr(_policy, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
