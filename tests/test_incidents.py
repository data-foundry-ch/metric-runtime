"""Incident model / opener wiring (no DuckDB)."""

from __future__ import annotations

from metric_runtime.models import Incident, IncidentState


def test_incident_fields():
    inc = Incident(
        primary_metric="weekend_profit",
        explanatory_kpi="basket_threshold_concentration",
        root_candidates=["basket_threshold_concentration"],
        scope={"city": "Amsterdam"},
        owner="Commercial Growth / Promotions",
        state=IncidentState.OPEN,
        first_detected="2026-05-15 12:00:00",
        estimated_impact=250.0,
        suppressed_ancestors=["weekend_profit", "profit_margin"],
    )
    assert "Promotions" in inc.owner
    assert inc.state == IncidentState.OPEN
