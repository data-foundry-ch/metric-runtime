"""Domain-neutral SQL / batch / derived calculation example.

Demonstrates first-class calculations without PyPizza domain measures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb

from metric_runtime import KPI, Formula, KPICatalog, KPIEngine
from metric_runtime.calculations import (
    BatchCalculation,
    BatchRegistry,
    DerivedCalculation,
    EvaluationContext,
    EvaluationSession,
    FormulaCalculation,
    SqlBatchSource,
    SqlCalculation,
)
from metric_runtime.execution import DuckDBExecutor

ROOT = Path(__file__).resolve().parent


def build_demo_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE opportunity (
            close_date TIMESTAMP,
            amount DOUBLE,
            is_won BOOLEAN,
            team VARCHAR
        );
        INSERT INTO opportunity VALUES
            ('2026-05-15 12:00:00', 120000, true, 'enterprise'),
            ('2026-05-15 12:00:00', 80000, true, 'enterprise'),
            ('2026-05-15 12:00:00', 30000, false, 'enterprise'),
            ('2026-05-15 12:00:00', 40000, true, 'smb');

        CREATE TABLE targets (
            as_of TIMESTAMP,
            team VARCHAR,
            bookings_target DOUBLE
        );
        INSERT INTO targets VALUES
            ('2026-05-15 12:00:00', 'enterprise', 250000),
            ('2026-05-15 12:00:00', 'smb', 100000);

        CREATE TABLE fact_orders (
            ts TIMESTAMP,
            orders DOUBLE,
            city VARCHAR
        );
        INSERT INTO fact_orders VALUES ('2026-05-15 12:00:00', 42, 'Amsterdam');
        """
    )
    return con


def build_catalog() -> KPICatalog:
    return KPICatalog(
        [
            KPI(
                name="orders",
                owner="ops",
                calculation=FormulaCalculation(formula=Formula.sum("orders")),
            ),
            KPI(
                name="bookings_target",
                owner="finance",
                calculation=SqlCalculation(
                    dialect="duckdb",
                    query="""
                        SELECT bookings_target AS value
                        FROM targets
                        WHERE as_of = :effective_at
                          AND team = :team
                    """,
                ),
            ),
            KPI(
                name="closed_won_revenue",
                owner="sales",
                calculation=BatchCalculation(
                    source="sales_metrics",
                    result="closed_won_revenue",
                ),
            ),
            KPI(
                name="slipped_value",
                owner="sales",
                calculation=BatchCalculation(
                    source="sales_metrics",
                    result="slipped_value",
                ),
            ),
            KPI(
                name="revenue_attainment",
                owner="finance",
                dependencies=("closed_won_revenue", "bookings_target"),
                calculation=DerivedCalculation(expression="closed_won_revenue / bookings_target"),
            ),
        ]
    )


def build_batch_registry() -> BatchRegistry:
    registry = BatchRegistry()
    registry.register(
        "sales_metrics",
        SqlBatchSource(
            "sales_metrics",
            dialect="duckdb",
            query="""
                SELECT
                    SUM(CASE WHEN is_won THEN amount ELSE 0 END) AS closed_won_revenue,
                    SUM(CASE WHEN NOT is_won THEN amount ELSE 0 END) AS slipped_value
                FROM opportunity
                WHERE close_date = :effective_at
                  AND team = :team
            """,
        ),
    )
    return registry


def main() -> None:
    con = build_demo_connection()
    catalog = build_catalog()
    registry = build_batch_registry()
    executor = DuckDBExecutor(con, fact_table="fact_orders")
    session = EvaluationSession(catalog, executor=executor, batch_registry=registry)

    ctx = EvaluationContext(
        effective_at=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        filters={"team": "enterprise"},
    )
    observations = session.evaluate_all(
        ["closed_won_revenue", "slipped_value", "revenue_attainment"],
        ctx,
    )
    for name, obs in observations.items():
        print(f"{name}={obs.value} status={obs.value_status}")

    # Formula KPIs use fact-table dimensions (not sales team filters).
    orders = session.evaluate(
        ["orders"],
        EvaluationContext(effective_at=ctx.effective_at, filters={"city": "Amsterdam"}),
    )["orders"]
    print(f"orders={orders.value} status={orders.value_status}")

    # Existing runtime still owns detection/state/incidents.
    engine = KPIEngine(
        catalog,
        executor=executor,
        batch_registry=registry,
    )
    print("orders via engine=", engine.metric_value("orders", at=ctx.effective_at))


if __name__ == "__main__":
    main()
