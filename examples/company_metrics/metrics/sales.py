"""Sales metrics for the company_metrics repository example."""

from __future__ import annotations

from metric_runtime import Metric, SqlCalculation

leads = Metric(
    id="leads",
    name="Leads",
    description="New sales leads created in the window.",
    calculation=SqlCalculation(
        dialect="duckdb",
        query="""
            SELECT COUNT(*)::DOUBLE AS value
            FROM lead
            WHERE created_at = :effective_at
              AND team = :team
        """,
    ),
    dimensions=["team", "country"],
    unit="count",
    owner="sales",
    tags={"sales"},
)

opportunities = Metric(
    id="opportunities",
    name="Opportunities",
    description="Open pipeline opportunities.",
    calculation=SqlCalculation(
        dialect="duckdb",
        query="""
            SELECT COUNT(*)::DOUBLE AS value
            FROM opportunity
            WHERE as_of = :as_of_date
              AND team = :team
              AND NOT is_won
        """,
    ),
    dimensions=["team", "country"],
    unit="count",
    owner="sales",
    tags={"sales"},
)

closed_won_revenue = Metric(
    id="closed_won_revenue",
    name="Closed-Won Revenue",
    description="Won opportunity amount for the evaluation date.",
    calculation=SqlCalculation(
        dialect="duckdb",
        query="""
            SELECT COALESCE(SUM(amount), 0)::DOUBLE AS value
            FROM opportunity
            WHERE is_won
              AND close_date = :effective_at
              AND team = :team
        """,
    ),
    dimensions=["team", "country"],
    unit="EUR",
    owner="sales",
    tags={"sales", "commercial"},
)

METRICS = [leads, opportunities, closed_won_revenue]
