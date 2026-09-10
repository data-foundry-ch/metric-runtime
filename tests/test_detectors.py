"""Detector unit tests."""

from __future__ import annotations

from datetime import UTC, datetime

from metric_runtime.detectors import SeasonalZScoreDetector, ThresholdDetector
from metric_runtime.models import DetectorConfig, Directionality


def test_seasonal_zscore_detects_drop():
    det = SeasonalZScoreDetector()
    status = det.evaluate(
        "m",
        current=50.0,
        baseline=[100.0, 102.0, 98.0, 101.0, 99.0, 100.0],
        directionality=Directionality.LOWER_IS_BAD,
        config=DetectorConfig(z_threshold=2.0, min_relative_change=0.1),
        support=100,
        support_ok=True,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert status.anomaly is True
    assert status.z_score < 0


def test_threshold_detector_absolute():
    thr = ThresholdDetector()
    status = thr.evaluate(
        "m",
        current=0.1,
        baseline=[0.2] * 6,
        directionality=Directionality.LOWER_IS_BAD,
        config=DetectorConfig(absolute_threshold=0.15, min_relative_change=0.0),
        support=50,
        support_ok=True,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert status.anomaly is True


def test_insufficient_support_blocks_anomaly():
    det = SeasonalZScoreDetector()
    status = det.evaluate(
        "m",
        current=1.0,
        baseline=[100.0] * 6,
        directionality=Directionality.LOWER_IS_BAD,
        config=DetectorConfig(z_threshold=1.0, min_relative_change=0.01),
        support=1,
        support_ok=False,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert status.anomaly is False
