"""Hardening: typed Calculation, freeze, metadata, process_many, impact rename."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from metric_runtime import (
    BatchCalculation,
    BatchRegistry,
    EvaluationContext,
    Formula,
    FormulaCalculation,
    InMemoryStateStore,
    KPIEngine,
    Metric,
    MetricCatalog,
)
from metric_runtime.calculations import CallableBatchSource
from metric_runtime.state import StatePolicy


def test_calculation_required_and_typed():
    with pytest.raises(ValidationError):
        Metric(id="x", name="X")
    m = Metric(id="orders", name="Orders", formula=Formula.sum("orders"))
    assert isinstance(m.calculation, FormulaCalculation)
    assert "formula" not in m.model_dump()
    assert "formula" not in m.model_dump(mode="json")
    restored = Metric.model_validate(m.model_dump(mode="json"))
    assert restored.calculation.kind == "formula"
    assert restored.formula is not None


def test_metric_is_frozen():
    m = Metric(id="orders", name="Orders", formula=Formula.sum("orders"))
    with pytest.raises(ValidationError):
        m.name = "Nope"  # type: ignore[misc]


def test_metadata_rejects_non_json():
    with pytest.raises(ValidationError):
        Metric(
            id="orders",
            name="Orders",
            formula=Formula.sum("orders"),
            metadata={"when": datetime.now(UTC)},
        )
    ok = Metric(
        id="orders",
        name="Orders",
        formula=Formula.sum("orders"),
        metadata={"domain": "ops", "flags": [1, True, None]},
    )
    assert ok.metadata["domain"] == "ops"


def test_process_many_shares_batch_source():
    calls = {"n": 0}

    def handler(ctx: EvaluationContext) -> dict[str, float | None]:
        calls["n"] += 1
        return {"a": 10.0, "b": 20.0}

    registry = BatchRegistry()
    registry.register("numbers", CallableBatchSource("numbers", handler))
    catalog = MetricCatalog(
        [
            Metric(
                id="a",
                name="A",
                calculation=BatchCalculation(source="numbers", result="a"),
            ),
            Metric(
                id="b",
                name="B",
                calculation=BatchCalculation(source="numbers", result="b"),
            ),
        ]
    )
    engine = KPIEngine(
        catalog,
        state_store=InMemoryStateStore(),
        batch_registry=registry,
        state_policy=StatePolicy(persistence=1, min_impact=0.0),
    )
    at = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    calls["n"] = 0
    engine.process("a", at=at)
    engine.process("b", at=at)
    separate_calls = calls["n"]

    calls["n"] = 0
    results = engine.process_many(["a", "b"], at=at)
    assert set(results) == {"a", "b"}
    assert results["a"].status.value == 10.0
    assert results["b"].status.value == 20.0
    assert calls["n"] < separate_calls
    assert calls["n"] >= 1


def test_estimate_impact_alias():
    catalog = MetricCatalog(
        [
            Metric(
                id="m",
                name="M",
                formula=Formula.sum("m"),
                impact={"kind": "margin_delta"},
            )
        ]
    )
    engine = KPIEngine(catalog, state_store=InMemoryStateStore())
    at = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    assert engine.estimate_impact("m", at, 80.0, 100.0) == 20.0
    assert engine.estimate_impact_eur("m", at, 80.0, 100.0) == 20.0
