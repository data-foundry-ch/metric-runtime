"""Tests for Formula / SQL / Batch / Derived calculations."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest

from metric_runtime import KPI, Formula, KPICatalog, KPIEngine, KPIState
from metric_runtime.calculations import (
    BatchCalculation,
    BatchRegistry,
    CallableBatchSource,
    DerivedCalculation,
    EvaluationContext,
    EvaluationSession,
    FormulaCalculation,
    ObservationValueStatus,
    SqlBatchSource,
    SqlCalculation,
)
from metric_runtime.exceptions import InvalidMetricDefinitionError, MetricRuntimeError
from metric_runtime.execution.duckdb import DuckDBExecutor
from metric_runtime.models import Directionality
from metric_runtime.state import StatePolicy
from metric_runtime.stores import InMemoryStateStore


def _ts(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _sales_db():
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE opportunity (
            close_date TIMESTAMP,
            amount DOUBLE,
            is_won BOOLEAN,
            team VARCHAR,
            stage VARCHAR
        );
        INSERT INTO opportunity VALUES
            ('2026-05-15 12:00:00', 100000, true, 'enterprise', 'closed'),
            ('2026-05-15 12:00:00', 50000, true, 'enterprise', 'closed'),
            ('2026-05-15 12:00:00', 20000, false, 'enterprise', 'open'),
            ('2026-05-15 12:00:00', 80000, true, 'smb', 'closed');
        CREATE TABLE targets (
            as_of TIMESTAMP,
            team VARCHAR,
            bookings_target DOUBLE
        );
        INSERT INTO targets VALUES
            ('2026-05-15 12:00:00', 'enterprise', 200000),
            ('2026-05-15 12:00:00', 'smb', 100000);
        CREATE TABLE fact_orders (
            ts TIMESTAMP,
            orders DOUBLE,
            city VARCHAR
        );
        INSERT INTO fact_orders VALUES
            ('2026-05-15 12:00:00', 10, 'Amsterdam');
        """
    )
    return con


def test_formula_calculation_still_works():
    con = _sales_db()
    catalog = KPICatalog(
        [
            KPI(
                name="orders",
                calculation=FormulaCalculation(formula=Formula.sum("orders")),
            )
        ]
    )
    engine = KPIEngine(
        catalog,
        executor=DuckDBExecutor(con, fact_table="fact_orders"),
    )
    assert engine.metric_value("orders", at=_ts(2026, 5, 15, 12, 0)) == 10.0


def test_formula_sugar_normalizes_to_calculation():
    metric = KPI(name="orders", formula=Formula.sum("orders"))
    assert isinstance(metric.calculation, FormulaCalculation)
    assert metric.calculation.formula.kind == "sum"


def test_sql_kpi_scalar_and_bindings():
    con = _sales_db()
    catalog = KPICatalog(
        [
            KPI(
                name="closed_won_revenue",
                calculation=SqlCalculation(
                    dialect="duckdb",
                    query="""
                        SELECT SUM(amount) AS value
                        FROM opportunity
                        WHERE is_won
                          AND close_date = :effective_at
                          AND team = :team
                    """,
                ),
            )
        ]
    )
    session = EvaluationSession(catalog, executor=DuckDBExecutor(con))
    ctx = EvaluationContext(
        effective_at=_ts(2026, 5, 15, 12, 0),
        filters={"team": "enterprise"},
    )
    obs = session.evaluate(["closed_won_revenue"], ctx)["closed_won_revenue"]
    assert obs.value == 150000.0
    assert obs.value_status == ObservationValueStatus.VALUE.value


def test_sql_kpi_local_bindings_and_context_override():
    con = _sales_db()
    catalog = KPICatalog(
        [
            KPI(
                name="aged_open",
                calculation=SqlCalculation(
                    dialect="duckdb",
                    query="""
                        SELECT COUNT(*)::DOUBLE AS value
                        FROM opportunity
                        WHERE NOT is_won
                          AND amount >= :aging_days
                          AND close_date = :effective_at
                    """,
                    bindings={"aging_days": 25},
                ),
            )
        ]
    )
    session = EvaluationSession(catalog, executor=DuckDBExecutor(con))
    # Uses KPI default aging_days=25 → open opportunity amount=20000 counts.
    default_obs = session.evaluate(
        ["aged_open"],
        EvaluationContext(effective_at=_ts(2026, 5, 15, 12, 0)),
    )["aged_open"]
    assert default_obs.value == 1.0

    override = session.evaluate(
        ["aged_open"],
        EvaluationContext(
            effective_at=_ts(2026, 5, 15, 12, 0),
            filters={"aging_days": 25000},
        ),
    )["aged_open"]
    assert override.value == 0.0

    metric = catalog.get("aged_open")
    restored = KPI.model_validate(metric.model_dump())
    assert restored.calculation.bindings == {"aging_days": 25}
    roundtrip = KPI.model_validate_json(metric.model_dump_json())
    assert roundtrip.calculation.bindings["aging_days"] == 25


def test_duckdb_executor_optional_fact_table():
    con = _sales_db()
    executor = DuckDBExecutor(con)
    assert executor.fact_table is None
    assert executor.execute_scalar("SELECT 42.0 AS value") == 42.0

    with pytest.raises(MetricRuntimeError, match="requires fact_table"):
        executor.metric_value(Formula.sum("orders"), at=_ts(2026, 5, 15, 12, 0))

    catalog = KPICatalog(
        [
            KPI(
                name="sql_only",
                calculation=SqlCalculation(
                    dialect="duckdb",
                    query="SELECT SUM(amount) AS value FROM opportunity WHERE is_won",
                ),
            )
        ]
    )
    session = EvaluationSession(catalog, executor=executor)
    obs = session.evaluate(
        ["sql_only"],
        EvaluationContext(effective_at=_ts(2026, 5, 15, 12, 0)),
    )["sql_only"]
    assert obs.value == 230000.0


def test_sql_null_is_no_data():
    con = _sales_db()
    catalog = KPICatalog(
        [
            KPI(
                name="missing",
                calculation=SqlCalculation(
                    dialect="duckdb",
                    query="""
                        SELECT SUM(amount) AS value
                        FROM opportunity
                        WHERE team = 'does-not-exist'
                    """,
                ),
            )
        ]
    )
    session = EvaluationSession(catalog, executor=DuckDBExecutor(con))
    result = session.calculate_value(
        "missing", EvaluationContext(effective_at=_ts(2026, 5, 15, 12, 0))
    )
    assert result.status == ObservationValueStatus.NO_DATA


def test_sql_missing_value_column_fails():
    con = _sales_db()
    catalog = KPICatalog(
        [
            KPI(
                name="bad",
                calculation=SqlCalculation(
                    dialect="duckdb",
                    query="SELECT SUM(amount) AS revenue FROM opportunity WHERE is_won",
                ),
            )
        ]
    )
    session = EvaluationSession(catalog, executor=DuckDBExecutor(con))
    result = session.calculate_value("bad", EvaluationContext(effective_at=_ts(2026, 5, 15, 12, 0)))
    assert result.status == ObservationValueStatus.ERROR
    assert "value" in (result.error or "")


def test_sql_multiple_rows_fail():
    con = _sales_db()
    catalog = KPICatalog(
        [
            KPI(
                name="multi",
                calculation=SqlCalculation(
                    dialect="duckdb",
                    query="SELECT amount AS value FROM opportunity WHERE is_won",
                ),
            )
        ]
    )
    session = EvaluationSession(catalog, executor=DuckDBExecutor(con))
    result = session.calculate_value(
        "multi", EvaluationContext(effective_at=_ts(2026, 5, 15, 12, 0))
    )
    assert result.status == ObservationValueStatus.ERROR
    assert "one row" in (result.error or "")


def test_sql_dialect_mismatch_fails_clearly():
    con = _sales_db()
    catalog = KPICatalog(
        [
            KPI(
                name="sf",
                calculation=SqlCalculation(
                    dialect="snowflake",
                    query="SELECT 1 AS value",
                ),
            )
        ]
    )
    session = EvaluationSession(catalog, executor=DuckDBExecutor(con))
    with pytest.raises(MetricRuntimeError, match="dialect"):
        session.calculate_value("sf", EvaluationContext(effective_at=_ts(2026, 5, 15, 12, 0)))


def test_batch_source_executes_once():
    calls = {"n": 0}

    def handler(ctx: EvaluationContext):
        calls["n"] += 1
        return {
            "closed_won_revenue": 100.0,
            "pipeline_coverage": 2.5,
            "slipped_value": 40.0,
        }

    registry = BatchRegistry()
    registry.register("sales_metrics", CallableBatchSource("sales_metrics", handler))
    catalog = KPICatalog(
        [
            KPI(
                name="closed_won_revenue",
                calculation=BatchCalculation(source="sales_metrics", result="closed_won_revenue"),
            ),
            KPI(
                name="pipeline_coverage",
                calculation=BatchCalculation(source="sales_metrics", result="pipeline_coverage"),
            ),
            KPI(
                name="slipped_value",
                calculation=BatchCalculation(source="sales_metrics", result="slipped_value"),
            ),
        ]
    )
    session = EvaluationSession(catalog, batch_registry=registry)
    ctx = EvaluationContext(effective_at=_ts(2026, 5, 15, 12, 0))
    obs = session.evaluate_all(["closed_won_revenue", "pipeline_coverage", "slipped_value"], ctx)
    assert calls["n"] == 1
    assert obs["closed_won_revenue"].value == 100.0
    assert obs["pipeline_coverage"].value == 2.5
    assert obs["slipped_value"].value == 40.0


def test_derived_division_and_topo_order():
    registry = BatchRegistry()
    registry.register(
        "numbers",
        CallableBatchSource(
            "numbers",
            lambda ctx: {"a": 100.0, "b": 20.0},
        ),
    )
    catalog = KPICatalog(
        [
            KPI(name="a", calculation=BatchCalculation(source="numbers", result="a")),
            KPI(name="b", calculation=BatchCalculation(source="numbers", result="b")),
            KPI(
                name="c",
                dependencies=("a", "b"),
                calculation=DerivedCalculation(expression="a / b"),
            ),
        ]
    )
    session = EvaluationSession(catalog, batch_registry=registry)
    result = session.calculate_value("c", EvaluationContext(effective_at=_ts(2026, 5, 15, 12, 0)))
    assert result.value == 5.0


def test_derived_division_by_zero_is_no_data():
    registry = BatchRegistry()
    registry.register(
        "numbers",
        CallableBatchSource("numbers", lambda ctx: {"a": 100.0, "b": 0.0}),
    )
    catalog = KPICatalog(
        [
            KPI(name="a", calculation=BatchCalculation(source="numbers", result="a")),
            KPI(name="b", calculation=BatchCalculation(source="numbers", result="b")),
            KPI(
                name="c",
                dependencies=("a", "b"),
                calculation=DerivedCalculation(expression="a / b"),
            ),
        ]
    )
    session = EvaluationSession(catalog, batch_registry=registry)
    result = session.calculate_value("c", EvaluationContext(effective_at=_ts(2026, 5, 15, 12, 0)))
    assert result.status == ObservationValueStatus.NO_DATA


def test_derived_rejects_hidden_dependencies():
    with pytest.raises(InvalidMetricDefinitionError, match="dependencies"):
        KPICatalog(
            [
                KPI(name="a", calculation=FormulaCalculation(formula=Formula.sum("orders"))),
                KPI(name="b", calculation=FormulaCalculation(formula=Formula.sum("orders"))),
                KPI(
                    name="c",
                    dependencies=("a",),
                    calculation=DerivedCalculation(expression="a / b"),
                ),
            ]
        )


def test_derived_rejects_arbitrary_python():
    with pytest.raises(InvalidMetricDefinitionError):
        KPICatalog(
            [
                KPI(name="a", calculation=FormulaCalculation(formula=Formula.sum("orders"))),
                KPI(
                    name="evil",
                    dependencies=("a",),
                    calculation=DerivedCalculation(expression="__import__('os').system('x')"),
                ),
            ]
        )


def test_mixed_calculation_graph_feeds_process(monkeypatch):
    con = _sales_db()
    registry = BatchRegistry()
    registry.register(
        "sales_metrics",
        SqlBatchSource(
            "sales_metrics",
            query="""
                SELECT
                    SUM(CASE WHEN is_won THEN amount ELSE 0 END) AS closed_won_revenue,
                    SUM(CASE WHEN NOT is_won THEN amount ELSE 0 END) AS slipped_value
                FROM opportunity
                WHERE close_date = :effective_at
                  AND team = :team
            """,
            dialect="duckdb",
        ),
    )
    catalog = KPICatalog(
        [
            KPI(
                name="closed_won_revenue",
                calculation=BatchCalculation(source="sales_metrics", result="closed_won_revenue"),
                directionality=Directionality.LOWER_IS_BAD,
            ),
            KPI(
                name="bookings_target",
                calculation=SqlCalculation(
                    dialect="duckdb",
                    query="""
                        SELECT bookings_target AS value
                        FROM targets
                        WHERE as_of = :effective_at AND team = :team
                    """,
                ),
            ),
            KPI(
                name="revenue_attainment",
                dependencies=("closed_won_revenue", "bookings_target"),
                calculation=DerivedCalculation(expression="closed_won_revenue / bookings_target"),
                directionality=Directionality.LOWER_IS_BAD,
            ),
        ]
    )
    engine = KPIEngine(
        catalog,
        executor=DuckDBExecutor(con),
        state_store=InMemoryStateStore(),
        batch_registry=registry,
        state_policy=StatePolicy(persistence=1, min_impact_eur=0.0),
    )
    at = _ts(2026, 5, 15, 12, 0)
    monkeypatch.setattr(engine, "estimate_impact_eur", lambda *a, **k: 100.0)
    # Force detector to see anomaly for process lifecycle smoke.
    monkeypatch.setattr(
        engine,
        "evaluate",
        lambda metric, at, filters=None: __import__(
            "metric_runtime.models", fromlist=["KPIStatus"]
        ).KPIStatus(
            name=metric,
            value=engine.metric_value(metric, at=at, filters=filters),
            baseline_mean=2.0,
            baseline_std=0.1,
            z_score=-5.0,
            relative_change=-0.5,
            anomaly=True,
            support=100,
            support_ok=True,
            as_of=at,
            directionality=Directionality.LOWER_IS_BAD,
            state=KPIState.DETECTED,
            severity=5.0,
        ),
    )
    result = engine.process(
        metric="revenue_attainment",
        at=at,
        scope={"team": "enterprise"},
    )
    assert result.status.value == 0.75
    assert result.transition.current in {KPIState.DETECTED, KPIState.OPEN}


def test_calculation_json_round_trip():
    metrics = [
        KPI(name="orders", calculation=FormulaCalculation(formula=Formula.sum("orders"))),
        KPI(
            name="sql_m",
            calculation=SqlCalculation(dialect="duckdb", query="SELECT 1 AS value"),
        ),
        KPI(
            name="batch_m",
            calculation=BatchCalculation(source="x", result="y"),
        ),
        KPI(
            name="derived_m",
            dependencies=("orders",),
            calculation=DerivedCalculation(expression="orders * 2"),
        ),
    ]
    for metric in metrics:
        restored = KPI.model_validate_json(metric.model_dump_json())
        assert restored.calculation == metric.calculation


def test_kpi_schema_exposes_calculation_union():
    from pydantic import TypeAdapter

    from metric_runtime.calculations.specs import Calculation

    schema = KPI.model_json_schema()
    assert "calculation" in schema["properties"]
    calc = TypeAdapter(Calculation).json_schema()
    blob = str(calc)
    assert "formula" in blob
    assert "sql" in blob
    assert "batch" in blob
    assert "derived" in blob
