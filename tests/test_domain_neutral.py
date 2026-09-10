"""Domain-neutral catalog / formula tests (no PyPizza knowledge)."""

from __future__ import annotations

import pytest

from metric_runtime import KPI, Formula, KPICatalog, SeasonalZScore, Threshold
from metric_runtime.models import Directionality, SupportRequirement, coerce_measure_ref


def test_formula_factories():
    s = Formula.sum("orders")
    assert s.kind == "sum" and s.measure == "orders"
    r = Formula.ratio("gross_revenue", "orders")
    assert r.numerator == "gross_revenue" and r.denominator == "orders"
    d = Formula.difference("revenue", "cost")
    assert d.left == "revenue" and d.right == "cost"


def test_invalid_measure_ref():
    with pytest.raises(ValueError):
        coerce_measure_ref("not a measure!")


def test_per_kpi_detector_instances():
    catalog = KPICatalog(
        [
            KPI(
                name="profit_margin",
                formula=Formula.ratio("profit", "revenue"),
                detector=SeasonalZScore(lookback_periods=6, threshold=3.0),
                directionality=Directionality.LOWER_IS_BAD,
            ),
            KPI(
                name="refund_rate",
                formula=Formula.ratio("refunds", "orders"),
                detector=Threshold(absolute_threshold=0.05, min_relative_change=0.0),
                directionality=Directionality.HIGHER_IS_BAD,
                support=SupportRequirement(measure="orders", minimum=10),
            ),
        ]
    )
    assert isinstance(catalog["profit_margin"].detector, SeasonalZScore)
    assert isinstance(catalog["refund_rate"].detector, Threshold)


def test_saas_style_dependency_graph():
    catalog = KPICatalog(
        [
            KPI(name="requests", formula=Formula.sum("requests")),
            KPI(
                name="customers",
                formula=Formula.sum("customers"),
                dependencies=("requests",),
            ),
            KPI(
                name="conversion_rate",
                formula=Formula.ratio("customers", "requests"),
                dependencies=("customers", "requests"),
            ),
            KPI(
                name="recurring_revenue",
                formula=Formula.sum("recurring_revenue"),
                dependencies=("customers",),
            ),
        ]
    )
    assert "customers" in catalog["recurring_revenue"].dependencies
