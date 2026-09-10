"""Serializable detector specifications and runtime factory.

KPI definitions store DetectorSpec models (JSON-safe).
KPIEngine builds DetectorStrategy instances via the registry.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from metric_runtime.detectors.base import (
    DetectorStrategy,
    SeasonalZScoreDetector,
    ThresholdDetector,
)
from metric_runtime.exceptions import MetricRuntimeError
from metric_runtime.models import DetectorConfig


class SeasonalZScore(BaseModel):
    """Serializable seasonal z-score detector policy."""

    type: Literal["seasonal_zscore"] = "seasonal_zscore"
    lookback_periods: int = Field(default=6, ge=3, le=12)
    threshold: float = Field(default=2.5, gt=0)
    min_relative_change: float = Field(default=0.08, ge=0)

    def to_runtime(self) -> SeasonalZScoreDetector:
        return SeasonalZScoreDetector(
            lookback_periods=self.lookback_periods,
            threshold=self.threshold,
            min_relative_change=self.min_relative_change,
        )

    def as_config(self) -> DetectorConfig:
        return DetectorConfig(
            baseline_weeks=self.lookback_periods,
            z_threshold=self.threshold,
            min_relative_change=self.min_relative_change,
        )


class Threshold(BaseModel):
    """Serializable absolute / relative threshold detector policy."""

    type: Literal["threshold"] = "threshold"
    lookback_periods: int = Field(default=6, ge=3, le=12)
    min_relative_change: float = Field(default=0.08, ge=0)
    absolute_threshold: float | None = None

    def to_runtime(self) -> ThresholdDetector:
        return ThresholdDetector(
            absolute_threshold=self.absolute_threshold,
            min_relative_change=self.min_relative_change,
            lookback_periods=self.lookback_periods,
        )

    def as_config(self) -> DetectorConfig:
        return DetectorConfig(
            baseline_weeks=self.lookback_periods,
            min_relative_change=self.min_relative_change,
            absolute_threshold=self.absolute_threshold,
        )


DetectorSpec = Annotated[
    SeasonalZScore | Threshold,
    Field(discriminator="type"),
]


def coerce_detector_spec(value: Any) -> Any:
    """Normalize DetectorConfig / legacy runtime instances into DetectorSpec."""
    if value is None:
        return SeasonalZScore()
    if isinstance(value, (SeasonalZScore, Threshold)):
        return value
    if isinstance(value, DetectorConfig):
        return SeasonalZScore(
            lookback_periods=value.baseline_weeks,
            threshold=value.z_threshold,
            min_relative_change=value.min_relative_change,
        )
    if isinstance(value, dict):
        dtype = value.get("type", "seasonal_zscore")
        if dtype == "threshold":
            return Threshold.model_validate(value)
        return SeasonalZScore.model_validate(value)
    if isinstance(value, DetectorStrategy):
        cfg = value.as_config()
        if getattr(value, "name", "") == "threshold":
            return Threshold(
                lookback_periods=cfg.baseline_weeks,
                min_relative_change=cfg.min_relative_change,
                absolute_threshold=cfg.absolute_threshold,
            )
        return SeasonalZScore(
            lookback_periods=cfg.baseline_weeks,
            threshold=cfg.z_threshold,
            min_relative_change=cfg.min_relative_change,
        )
    raise TypeError(
        f"detector must be a DetectorSpec (SeasonalZScore | Threshold), got {type(value)!r}"
    )


def build_detector(spec: DetectorSpec | DetectorConfig | None) -> DetectorStrategy:
    """Build a runtime detector from a serializable specification."""
    normalized = coerce_detector_spec(spec)
    if isinstance(normalized, SeasonalZScore):
        return normalized.to_runtime()
    if isinstance(normalized, Threshold):
        return normalized.to_runtime()
    raise MetricRuntimeError(f"Unknown detector specification: {normalized!r}")


# Back-compat aliases used by older call sites / talk material.
SeasonalZScoreSpec = SeasonalZScore
ThresholdSpec = Threshold
