"""Tests for presentation bands, display fields, catalog interchange, aliases."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from metric_runtime import (
    KPI,
    BatchCalculation,
    DerivedCalculation,
    Directionality,
    EvaluationContext,
    Formula,
    FormulaCalculation,
    KPICatalog,
    KPIObservation,
    PresentationBand,
    PresentationThreshold,
    SqlCalculation,
    classify_presentation_band,
)
from metric_runtime.exceptions import InvalidMetricDefinitionError


def test_presentation_band_higher_is_better():
    thr = PresentationThreshold(target=100.0, warning=90.0, critical=70.0)
    assert (
        classify_presentation_band(95.0, thresholds=thr, directionality=Directionality.LOWER_IS_BAD)
        == PresentationBand.ON_TARGET
    )
    assert (
        classify_presentation_band(80.0, thresholds=thr, directionality=Directionality.LOWER_IS_BAD)
        == PresentationBand.AT_RISK
    )
    assert (
        classify_presentation_band(60.0, thresholds=thr, directionality=Directionality.LOWER_IS_BAD)
        == PresentationBand.OFF_TARGET
    )


def test_presentation_band_lower_is_better():
    thr = PresentationThreshold(target=5.0, warning=10.0, critical=20.0)
    assert (
        classify_presentation_band(4.0, thresholds=thr, directionality=Directionality.HIGHER_IS_BAD)
        == PresentationBand.ON_TARGET
    )
    assert (
        classify_presentation_band(
            15.0, thresholds=thr, directionality=Directionality.HIGHER_IS_BAD
        )
        == PresentationBand.AT_RISK
    )
    assert (
        classify_presentation_band(
            25.0, thresholds=thr, directionality=Directionality.HIGHER_IS_BAD
        )
        == PresentationBand.OFF_TARGET
    )


def test_presentation_band_two_sided_and_no_data():
    thr = PresentationThreshold(target=50.0, warning=5.0, critical=15.0)
    assert (
        classify_presentation_band(52.0, thresholds=thr, directionality=Directionality.TWO_SIDED)
        == PresentationBand.ON_TARGET
    )
    assert (
        classify_presentation_band(60.0, thresholds=thr, directionality=Directionality.TWO_SIDED)
        == PresentationBand.AT_RISK
    )
    assert (
        classify_presentation_band(80.0, thresholds=thr, directionality=Directionality.TWO_SIDED)
        == PresentationBand.OFF_TARGET
    )
    assert (
        classify_presentation_band(None, thresholds=thr, directionality=Directionality.LOWER_IS_BAD)
        == PresentationBand.NO_DATA
    )


def test_kpi_display_fields():
    metric = KPI(
        name="revenue_attainment",
        label="Revenue Attainment",
        unit="percent",
        format="0.0%",
        formula=Formula.ratio("closed_won_revenue", "bookings_target"),
        dependencies=("closed_won_revenue", "bookings_target"),
        metadata={"presentation": {"graph_ring": 2}},
    )
    assert metric.display() == {
        "label": "Revenue Attainment",
        "unit": "percent",
        "format": "0.0%",
    }
    assert metric.presentation() == {"graph_ring": 2}
    restored = KPI.model_validate_json(metric.model_dump_json())
    assert restored.format == "0.0%"
    assert restored.unit == "percent"


def test_evaluation_context_as_of_date_alias():
    at = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    ctx = EvaluationContext(effective_at=at, filters={"team": "enterprise"})
    bindings = ctx.bindings()
    assert bindings["effective_at"] == at
    assert bindings["as_of_date"] == at
    assert bindings["at"] == at
    assert bindings["start_date"] == at
    assert bindings["end_date"] == at
    assert bindings["team"] == "enterprise"


def test_observation_null_semantics():
    at = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    measured = KPIObservation(name="m", value=0.0, as_of=at, value_status="value")
    assert measured.has_value is True
    assert measured.measured_value == 0.0
    assert measured.is_no_data is False

    missing = KPIObservation(name="m", value=0.0, as_of=at, value_status="no_data")
    assert missing.has_value is False
    assert missing.measured_value is None
    assert missing.is_no_data is True

    errored = KPIObservation(
        name="m",
        value=0.0,
        as_of=at,
        value_status="error",
        calculation_error="boom",
    )
    assert errored.measured_value is None
    assert errored.is_error is True
    assert errored.calculation_error == "boom"


def test_catalog_json_and_jsonl_round_trip(tmp_path):
    catalog = KPICatalog(
        [
            KPI(
                name="orders",
                calculation=FormulaCalculation(formula=Formula.sum("orders")),
                unit="count",
                format="#,##0",
            ),
            KPI(
                name="closed_won_revenue",
                calculation=SqlCalculation(
                    dialect="duckdb",
                    query="SELECT SUM(amount) AS value FROM opportunity WHERE is_won",
                    bindings={"aging_days": 30},
                ),
                unit="eur",
            ),
            KPI(
                name="batch_m",
                calculation=BatchCalculation(source="sales_metrics", result="closed_won_revenue"),
            ),
            KPI(
                name="attainment",
                dependencies=("closed_won_revenue", "batch_m"),
                calculation=DerivedCalculation(expression="closed_won_revenue / batch_m"),
                unit="ratio",
                format="0.00",
            ),
        ]
    )

    schema = KPICatalog.json_schema()
    assert schema["type"] == "array"
    assert "items" in schema

    restored_json = KPICatalog.from_json(catalog.to_json())
    assert set(restored_json.names()) == set(catalog.names())
    assert restored_json["orders"].calculation == catalog["orders"].calculation
    assert restored_json["closed_won_revenue"].calculation.bindings == {"aging_days": 30}
    assert restored_json["attainment"].calculation.expression == "closed_won_revenue / batch_m"
    assert restored_json["orders"].format == "#,##0"

    restored_jsonl = KPICatalog.from_jsonl(catalog.to_jsonl())
    assert set(restored_jsonl.names()) == set(catalog.names())
    assert restored_jsonl["batch_m"].calculation.source == "sales_metrics"

    path = tmp_path / "catalog.jsonl"
    catalog.write_jsonl(path)
    from_disk = KPICatalog.read_jsonl(path)
    assert len(from_disk) == 4


def test_catalog_from_json_rejects_object():
    with pytest.raises(InvalidMetricDefinitionError, match="array"):
        KPICatalog.from_json('{"name": "x"}')
