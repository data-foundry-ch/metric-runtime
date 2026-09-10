"""Public API smoke tests — import only from metric_runtime."""

from __future__ import annotations

from datetime import UTC, datetime

from metric_runtime import (
    KPI,
    Formula,
    InMemoryStateStore,
    KPICatalog,
    KPIEngine,
    KPIObservation,
    KPIState,
    SeasonalZScore,
)
from metric_runtime.models import Directionality


def test_public_import_surface():
    assert KPIState.NORMAL.value == "NORMAL"
    obs = KPIObservation(name="x", value=1.0, as_of=datetime(2026, 1, 1, tzinfo=UTC))
    assert obs.value == 1.0


def test_readme_style_zero_infra_example():
    """metric-runtime itself can be understood without DuckDB or a domain enum."""
    requests = KPI(
        name="requests",
        owner="growth",
        formula=Formula.sum("requests"),
        directionality=Directionality.TWO_SIDED,
    )
    customers = KPI(
        name="customers",
        owner="growth",
        formula=Formula.sum("customers"),
        dependencies=("requests",),
        directionality=Directionality.TWO_SIDED,
    )
    conversion_rate = KPI(
        name="conversion_rate",
        owner="growth",
        formula=Formula.ratio("customers", "requests"),
        dependencies=("customers", "requests"),
        directionality=Directionality.LOWER_IS_BAD,
        detector=SeasonalZScore(lookback_periods=6, threshold=3.0),
    )
    recurring_revenue = KPI(
        name="recurring_revenue",
        owner="finance",
        formula=Formula.sum("recurring_revenue"),
        dependencies=("customers",),
        directionality=Directionality.LOWER_IS_BAD,
        detector=SeasonalZScore(lookback_periods=6, threshold=2.5),
    )
    catalog = KPICatalog([requests, customers, conversion_rate, recurring_revenue])
    engine = KPIEngine(catalog=catalog, state_store=InMemoryStateStore())
    assert len(engine.catalog) == 4
    assert engine.state_store.get_state("recurring_revenue") == KPIState.NORMAL
    assert isinstance(catalog["conversion_rate"].detector, SeasonalZScore)


def test_import_has_no_side_effects():
    import metric_runtime

    assert metric_runtime.__version__ == "0.1.0"
