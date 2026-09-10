"""Model serialization / naming tests."""

from __future__ import annotations

from metric_runtime.detectors import SeasonalZScore, Threshold
from metric_runtime.models import (
    KPI,
    Detection,
    Directionality,
    Formula,
    Incident,
    IncidentState,
    KPIObservation,
    KPIState,
    KPIStatus,
)


def test_kpi_display_name_default():
    k = KPI(name="profit_margin", owner="finance")
    assert k.display_name == "Profit Margin"


def test_kpi_json_round_trip():
    metric = KPI(
        name="conversion_rate",
        owner="growth",
        formula=Formula.ratio("customers", "requests"),
        dependencies=("customers", "requests"),
        directionality=Directionality.LOWER_IS_BAD,
        detector=SeasonalZScore(lookback_periods=6, threshold=3.0),
        metadata={"presentation": {"graph_ring": 1, "graph_side": "marketing"}},
    )
    encoded = metric.model_dump_json()
    restored = KPI.model_validate_json(encoded)
    assert restored == metric
    assert isinstance(restored.detector, SeasonalZScore)
    assert restored.detector.threshold == 3.0


def test_kpi_json_round_trip_threshold_detector():
    metric = KPI(
        name="error_rate",
        owner="platform",
        formula=Formula.ratio("errors", "requests"),
        detector=Threshold(absolute_threshold=0.05, min_relative_change=0.0),
    )
    restored = KPI.model_validate_json(metric.model_dump_json())
    assert restored == metric
    assert isinstance(restored.detector, Threshold)


def test_kpi_model_json_schema():
    schema = KPI.model_json_schema()
    assert schema["type"] == "object"
    assert "name" in schema["properties"]
    assert "detector" in schema["properties"]
    assert "formula" in schema["properties"]
    # Presentation layout is not a first-class KPI field.
    assert "graph_ring" not in schema["properties"]
    assert "graph_side" not in schema["properties"]


def test_status_to_observation_and_detection():
    status = KPIStatus(
        name="m",
        value=1.0,
        baseline_mean=2.0,
        baseline_std=0.1,
        z_score=-10.0,
        relative_change=-0.5,
        anomaly=True,
        support=100,
        support_ok=True,
        as_of="t",
        directionality=Directionality.LOWER_IS_BAD,
        state=KPIState.DETECTED,
        severity=5.0,
    )
    obs = status.to_observation({"city": "Amsterdam"})
    det = status.to_detection()
    assert isinstance(obs, KPIObservation)
    assert isinstance(det, Detection)
    assert obs.filters["city"] == "Amsterdam"
    assert det.anomalous is True


def test_incident_backcompat_aliases():
    inc = Incident(
        primary_metric="weekend_profit",
        explanatory_kpi="basket_threshold_concentration",
        owner="Promotions",
        state=IncidentState.OPEN,
        first_detected="t",
        estimated_impact=120.0,
    )
    assert inc.kpi == "weekend_profit"
    assert inc.impact_eur == 120.0
