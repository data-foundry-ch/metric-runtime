"""Engine without YAML / without DuckDB."""

from __future__ import annotations

from metric_runtime import KPI, InMemoryStateStore, KPICatalog, KPIEngine
from metric_runtime.exceptions import MetricRuntimeError
from metric_runtime.models import Formula, Measure


def test_engine_python_api_without_yaml():
    catalog = KPICatalog(
        [
            KPI(
                name="orders",
                formula=Formula(kind="sum", measure=Measure.ORDERS),
            )
        ]
    )
    engine = KPIEngine(catalog=catalog, state_store=InMemoryStateStore())
    assert engine.executor is None
    try:
        engine.evaluate("orders", __import__("datetime").datetime(2026, 1, 1))
        raised = False
    except MetricRuntimeError:
        raised = True
    assert raised
