"""Engine without YAML / without DuckDB."""

from __future__ import annotations

from metric_runtime import KPI, Formula, InMemoryStateStore, KPICatalog, KPIEngine
from metric_runtime.exceptions import MetricRuntimeError


def test_engine_python_api_without_yaml():
    catalog = KPICatalog(
        [
            KPI(
                name="requests",
                formula=Formula.sum("requests"),
            )
        ]
    )
    engine = KPIEngine(catalog=catalog, state_store=InMemoryStateStore())
    assert engine.executor is None
    try:
        engine.evaluate("requests", __import__("datetime").datetime(2026, 1, 1))
        raised = False
    except MetricRuntimeError:
        raised = True
    assert raised


def test_support_without_requirement_does_not_assume_orders():
    catalog = KPICatalog([KPI(name="latency", formula=Formula.sum("latency_ms"))])
    engine = KPIEngine(catalog=catalog, state_store=InMemoryStateStore())
    assert engine.support_value("latency") == 0.0
