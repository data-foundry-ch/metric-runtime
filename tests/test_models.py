"""Model serialization / naming tests."""

from __future__ import annotations

from metric_runtime.models import (
    KPI,
    Detection,
    Directionality,
    Incident,
    IncidentState,
    KPIObservation,
    KPIState,
    KPIStatus,
)


def test_kpi_display_name_default():
    k = KPI(name="profit_margin", owner="finance")
    assert k.display_name == "Profit Margin"


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
