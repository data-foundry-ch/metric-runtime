"""Pluggable abnormality detectors.

The z-score is one detector implementation, not the architecture.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from statistics import mean, pstdev
from typing import Protocol, runtime_checkable

from metric_runtime.models import (
    Detection,
    DetectorConfig,
    Directionality,
    KPIObservation,
    KPIState,
    KPIStatus,
)


@runtime_checkable
class Detector(Protocol):
    """Pluggable abnormality test."""

    name: str

    def detect(
        self,
        observation: KPIObservation,
        history: Sequence[KPIObservation],
        *,
        directionality: Directionality = Directionality.TWO_SIDED,
        config: DetectorConfig | None = None,
    ) -> Detection: ...


class DetectorStrategy(ABC):
    """Legacy-compatible detector base used by KPIEngine.evaluate."""

    name: str = "detector"

    @abstractmethod
    def evaluate(
        self,
        metric_name: str,
        current: float,
        baseline: list[float],
        *,
        directionality: Directionality,
        config: DetectorConfig,
        support: float,
        support_ok: bool,
        as_of: str,
    ) -> KPIStatus:
        raise NotImplementedError

    def detect(
        self,
        observation: KPIObservation,
        history: Sequence[KPIObservation],
        *,
        directionality: Directionality = Directionality.TWO_SIDED,
        config: DetectorConfig | None = None,
    ) -> Detection:
        cfg = config or DetectorConfig()
        baseline = [h.value for h in history] or list(observation.baseline_values)
        status = self.evaluate(
            observation.name,
            observation.value,
            baseline,
            directionality=directionality,
            config=cfg,
            support=observation.support,
            support_ok=observation.support_ok,
            as_of=observation.as_of,
        )
        return status.to_detection()


def _directional_anomaly(
    current: float,
    baseline_mean: float,
    directionality: Directionality,
) -> bool:
    if directionality == Directionality.LOWER_IS_BAD:
        return current < baseline_mean
    if directionality == Directionality.HIGHER_IS_BAD:
        return current > baseline_mean
    return True


def _severity(z_score: float, relative_change: float) -> float:
    return abs(z_score) * 0.5 + abs(relative_change) * 10.0


class SeasonalZScoreDetector(DetectorStrategy):
    """Same weekday / same interval over prior N weeks."""

    name = "seasonal_zscore"

    def __init__(
        self,
        lookback_periods: int | None = None,
        threshold: float | None = None,
    ) -> None:
        self.lookback_periods = lookback_periods
        self.threshold = threshold

    def evaluate(
        self,
        metric_name: str,
        current: float,
        baseline: list[float],
        *,
        directionality: Directionality,
        config: DetectorConfig,
        support: float,
        support_ok: bool,
        as_of: str,
    ) -> KPIStatus:
        z_threshold = self.threshold if self.threshold is not None else config.z_threshold
        effective = config.model_copy(update={"z_threshold": z_threshold})

        baseline_mean = mean(baseline) if baseline else 0.0
        baseline_std = (pstdev(baseline) if len(baseline) > 1 else 0.0) or max(
            abs(baseline_mean) * 0.001, 1e-9
        )
        z_score = (current - baseline_mean) / baseline_std
        relative_change = (current - baseline_mean) / baseline_mean if baseline_mean != 0 else 0.0

        magnitude_ok = (
            abs(z_score) >= effective.z_threshold
            and abs(relative_change) >= effective.min_relative_change
        )
        anomaly = False
        if support_ok and magnitude_ok:
            anomaly = _directional_anomaly(current, baseline_mean, directionality)

        return KPIStatus(
            name=metric_name,
            value=current,
            baseline_mean=baseline_mean,
            baseline_std=baseline_std,
            z_score=z_score,
            relative_change=relative_change,
            anomaly=anomaly,
            support=support,
            support_ok=support_ok,
            as_of=as_of,
            directionality=directionality,
            state=KPIState.DETECTED if anomaly else KPIState.NORMAL,
            severity=_severity(z_score, relative_change) if anomaly else 0.0,
        )


# Back-compat alias used by older imports / talk slides.
SeasonalBaselineDetector = SeasonalZScoreDetector


class ThresholdDetector(DetectorStrategy):
    """Simple absolute / relative threshold — proves detectors are swappable."""

    name = "threshold"

    def evaluate(
        self,
        metric_name: str,
        current: float,
        baseline: list[float],
        *,
        directionality: Directionality,
        config: DetectorConfig,
        support: float,
        support_ok: bool,
        as_of: str,
    ) -> KPIStatus:
        baseline_mean = mean(baseline) if baseline else 0.0
        baseline_std = (pstdev(baseline) if len(baseline) > 1 else 0.0) or max(
            abs(baseline_mean) * 0.001, 1e-9
        )
        z_score = (current - baseline_mean) / baseline_std
        relative_change = (current - baseline_mean) / baseline_mean if baseline_mean != 0 else 0.0

        anomaly = False
        if support_ok:
            if config.absolute_threshold is not None:
                if directionality == Directionality.LOWER_IS_BAD:
                    anomaly = current < config.absolute_threshold
                elif directionality == Directionality.HIGHER_IS_BAD:
                    anomaly = current > config.absolute_threshold
                else:
                    anomaly = abs(current - config.absolute_threshold) > 0
            else:
                anomaly = abs(
                    relative_change
                ) >= config.min_relative_change and _directional_anomaly(
                    current, baseline_mean, directionality
                )

        return KPIStatus(
            name=metric_name,
            value=current,
            baseline_mean=baseline_mean,
            baseline_std=baseline_std,
            z_score=z_score,
            relative_change=relative_change,
            anomaly=anomaly,
            support=support,
            support_ok=support_ok,
            as_of=as_of,
            directionality=directionality,
            state=KPIState.DETECTED if anomaly else KPIState.NORMAL,
            severity=_severity(z_score, relative_change) if anomaly else 0.0,
        )
