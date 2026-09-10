"""Public API smoke tests — import only from metric_runtime."""

from __future__ import annotations

from metric_runtime import (
    KPI,
    InMemoryStateStore,
    KPICatalog,
    KPIEngine,
    KPIObservation,
    KPIState,
)
from metric_runtime.models import DetectorConfig, Directionality, Formula, Measure


def test_public_import_surface():
    assert KPIState.NORMAL.value == "NORMAL"
    obs = KPIObservation(name="x", value=1.0, as_of="t")
    assert obs.value == 1.0


def test_readme_style_zero_infra_example():
    """metric-runtime itself can be understood without DuckDB."""
    aov = KPI(
        name="average_order_value",
        owner="commercial-growth",
        formula=Formula(
            kind="ratio",
            numerator=Measure.GROSS_ORDER_VALUE,
            denominator=Measure.ORDERS,
        ),
        directionality=Directionality.LOWER_IS_BAD,
        detector=DetectorConfig(baseline_weeks=6, z_threshold=3.0),
    )
    cost = KPI(
        name="average_cost_per_order",
        owner="commercial-operations",
        formula=Formula(
            kind="ratio",
            numerator=Measure.PLATFORM_COST,
            denominator=Measure.ORDERS,
        ),
        directionality=Directionality.HIGHER_IS_BAD,
    )
    profit_margin = KPI(
        name="profit_margin",
        owner="commercial-finance",
        dependencies=["average_order_value", "average_cost_per_order"],
        directionality=Directionality.LOWER_IS_BAD,
        detector=DetectorConfig(baseline_weeks=6, z_threshold=3.0),
    )
    catalog = KPICatalog([aov, cost, profit_margin])
    engine = KPIEngine(catalog=catalog, state_store=InMemoryStateStore())
    assert len(engine.catalog) == 3
    assert engine.state_store.get_state("profit_margin") == KPIState.NORMAL
    assert "average_order_value" in catalog


def test_import_has_no_side_effects():
    import metric_runtime

    assert metric_runtime.__version__ == "0.1.0"
