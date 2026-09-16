"""Tests for Metric / MetricCatalog repository APIs."""

from __future__ import annotations

import pytest

from metric_runtime import (
    DerivedCalculation,
    Directionality,
    Formula,
    FormulaCalculation,
    Metric,
    MetricCatalog,
    SqlCalculation,
    UnitSpec,
)
from metric_runtime.exceptions import (
    DependencyCycleError,
    InvalidMetricDefinitionError,
    UnknownDependencyError,
)
from metric_runtime.ids import validate_metric_id


def test_metric_id_vs_display_name():
    metric = Metric(
        id="profit_margin",
        name="Profit Margin",
        calculation=DerivedCalculation(expression="profit / revenue"),
        dependencies=["profit", "revenue"],
    )
    renamed = metric.model_copy(update={"name": "Contribution Margin %"})
    assert renamed.id == "profit_margin"
    assert renamed.name == "Contribution Margin %"
    assert renamed.semantic_hash() == metric.semantic_hash()
    assert renamed.content_hash() != metric.content_hash()


def test_metric_id_rules():
    validate_metric_id("profit_margin")
    validate_metric_id("aov_30d")
    with pytest.raises(InvalidMetricDefinitionError):
        validate_metric_id("Profit Margin")
    with pytest.raises(InvalidMetricDefinitionError):
        validate_metric_id("profit-margin")
    with pytest.raises(InvalidMetricDefinitionError):
        Metric(id="finance.profit", name="x")


def test_duplicate_ids_fail():
    with pytest.raises(InvalidMetricDefinitionError, match="Duplicate"):
        MetricCatalog(
            [
                Metric(id="a", name="A", formula=Formula.sum("orders")),
                Metric(id="a", name="A2", formula=Formula.sum("orders")),
            ]
        )


def test_unknown_dependency_suggests():
    with pytest.raises(UnknownDependencyError, match="Did you mean .revenue"):
        MetricCatalog(
            [
                Metric(id="revenue", name="Revenue", formula=Formula.sum("revenue")),
                Metric(id="profit", name="Profit", formula=Formula.sum("profit")),
                Metric(
                    id="profit_margin",
                    name="Profit Margin",
                    calculation=DerivedCalculation(expression="profit / revenue"),
                    dependencies=["profit", "reveneu"],
                ),
            ]
        )


def test_cycle_rejected():
    with pytest.raises(DependencyCycleError):
        MetricCatalog(
            [
                Metric(id="a", name="A", dependencies=["b"], formula=Formula.sum("a")),
                Metric(id="b", name="B", dependencies=["c"], formula=Formula.sum("b")),
                Metric(id="c", name="C", dependencies=["a"], formula=Formula.sum("c")),
            ]
        )


def test_extensible_units():
    for unit in (
        "EUR",
        "CHF",
        "percent",
        "days",
        "milliseconds",
        "score",
        "requests_per_second",
    ):
        m = Metric(id="m", name="M", unit=unit, formula=Formula.sum("x"))
        assert isinstance(m.unit, UnitSpec)
        assert m.unit.id in {unit, "EUR", "percent"} or m.unit.id == unit


def test_catalog_graph_ops():
    catalog = MetricCatalog(
        [
            Metric(id="revenue", name="Revenue", formula=Formula.sum("revenue")),
            Metric(id="cost", name="Cost", formula=Formula.sum("cost")),
            Metric(
                id="profit",
                name="Profit",
                calculation=DerivedCalculation(expression="revenue - cost"),
                dependencies=["revenue", "cost"],
            ),
            Metric(
                id="profit_margin",
                name="Profit Margin",
                calculation=DerivedCalculation(expression="profit / revenue"),
                dependencies=["profit", "revenue"],
                unit="percent",
                owner="finance",
                directionality=Directionality.LOWER_IS_BAD,
            ),
        ]
    )
    assert catalog.dependencies("profit_margin") == ["profit", "revenue"] or set(
        catalog.dependencies("profit_margin")
    ) == {"profit", "revenue"}
    assert "profit_margin" in catalog.dependents("profit")
    assert "revenue" in catalog.ancestors("profit_margin")
    assert catalog.topological_order()[0] in {"revenue", "cost"}
    sub = catalog.subgraph("profit_margin")
    assert set(sub.ids()) == {"revenue", "cost", "profit", "profit_margin"}


def test_deterministic_export_and_round_trip():
    metrics = [
        Metric(id="c", name="C", formula=Formula.sum("c")),
        Metric(id="a", name="A", formula=Formula.sum("a")),
        Metric(
            id="b",
            name="B",
            calculation=SqlCalculation(dialect="duckdb", query="SELECT 1 AS value"),
        ),
        Metric(
            id="d",
            name="D",
            calculation=DerivedCalculation(expression="a / b"),
            dependencies=["a", "b"],
        ),
    ]
    catalog = MetricCatalog(metrics)
    assert [m.id for m in catalog.to_snapshot().metrics] == ["a", "b", "c", "d"]
    assert catalog.to_json() == catalog.to_json()
    assert catalog.to_jsonl() == catalog.to_jsonl()

    restored = MetricCatalog.from_json(catalog.to_json())
    assert restored.semantic_hash() == catalog.semantic_hash()
    assert restored["d"].calculation.expression == "a / b"

    restored_l = MetricCatalog.from_jsonl(catalog.to_jsonl())
    assert set(restored_l.ids()) == {"a", "b", "c", "d"}


def test_semantic_hash_policy():
    base = Metric(
        id="m",
        name="Margin",
        calculation=DerivedCalculation(expression="a / b"),
        dependencies=["a", "b"],
        unit="percent",
        owner="finance",
    )
    # Display-only changes do not affect semantic_hash.
    assert (
        base.model_copy(
            update={"name": "New Name", "description": "x", "tags": ["t"]}
        ).semantic_hash()
        == base.semantic_hash()
    )
    # Calculation / deps do.
    assert (
        base.model_copy(
            update={"calculation": DerivedCalculation(expression="a - b")}
        ).semantic_hash()
        != base.semantic_hash()
    )
    assert (
        base.model_copy(update={"dependencies": ("b", "a")}).semantic_hash() != base.semantic_hash()
    )


def test_catalog_diff():
    old = MetricCatalog(
        [
            Metric(id="keep", name="Keep", formula=Formula.sum("k")),
            Metric(id="gone", name="Gone", formula=Formula.sum("g")),
            Metric(
                id="chg",
                name="Chg",
                calculation=FormulaCalculation(formula=Formula.sum("x")),
            ),
        ]
    )
    new = MetricCatalog(
        [
            Metric(id="keep", name="Keep", formula=Formula.sum("k")),
            Metric(id="added", name="Added", formula=Formula.sum("a")),
            Metric(
                id="chg",
                name="Chg Renamed",
                calculation=FormulaCalculation(formula=Formula.sum("y")),
            ),
        ]
    )
    diff = old.diff(new)
    assert diff.added == ["added"]
    assert diff.removed == ["gone"]
    assert "calculation" in diff.changed["chg"]
    assert "name" in diff.changed["chg"]


def test_module_entrypoint_loading(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    metrics_pkg = root / "metrics"
    metrics_pkg.mkdir(parents=True)
    (metrics_pkg / "__init__.py").write_text("", encoding="utf-8")
    (metrics_pkg / "finance.py").write_text(
        """
from metric_runtime import Metric, Formula, DerivedCalculation
revenue = Metric(id="revenue", name="Revenue", formula=Formula.sum("revenue"))
cost = Metric(id="cost", name="Cost", formula=Formula.sum("cost"))
profit = Metric(
    id="profit",
    name="Profit",
    calculation=DerivedCalculation(expression="revenue - cost"),
    dependencies=["revenue", "cost"],
)
METRICS = [revenue, cost, profit]
""",
        encoding="utf-8",
    )
    (metrics_pkg / "catalog.py").write_text(
        """
from metric_runtime import MetricCatalog
from .finance import METRICS
catalog = MetricCatalog(METRICS, name="demo")
""",
        encoding="utf-8",
    )
    (root / "metric-runtime.yaml").write_text(
        """
project:
  name: demo
catalog:
  entrypoint: metrics.catalog:catalog
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(root))
    from metric_runtime.config.factory import load_catalog_entrypoint

    catalog = load_catalog_entrypoint("metrics.catalog:catalog", base_dir=root)
    assert len(catalog) == 3
    assert catalog.name == "demo"
    assert catalog["profit"].dependencies == ("revenue", "cost")


def test_catalog_markdown_and_schema():
    catalog = MetricCatalog(
        [Metric(id="orders", name="Orders", formula=Formula.sum("orders"), unit="count")]
    )
    md = catalog.to_markdown()
    assert "orders" in md
    assert "Orders" in md
    schema = MetricCatalog.json_schema()
    assert schema["properties"]["metrics"]["items"]
    assert "id" in Metric.model_json_schema()["properties"]
