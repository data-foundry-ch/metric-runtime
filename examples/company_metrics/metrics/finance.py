"""Finance metrics for the company_metrics repository example."""

from __future__ import annotations

from metric_runtime import (
    DerivedCalculation,
    Directionality,
    Formula,
    FormulaCalculation,
    Metric,
    SeasonalZScore,
)

revenue = Metric(
    id="revenue",
    name="Revenue",
    description="Recognized revenue for the evaluation window.",
    calculation=FormulaCalculation(formula=Formula.sum("revenue")),
    dimensions=["country", "channel"],
    unit="EUR",
    owner="finance",
    tags={"finance", "executive"},
)

cost = Metric(
    id="cost",
    name="Cost",
    description="Fully loaded cost of goods and delivery.",
    calculation=FormulaCalculation(formula=Formula.sum("cost")),
    dimensions=["country", "channel"],
    unit="EUR",
    owner="finance",
    tags={"finance"},
)

profit = Metric(
    id="profit",
    name="Profit",
    description="Revenue minus cost.",
    calculation=DerivedCalculation(expression="revenue - cost"),
    dependencies=["revenue", "cost"],
    dimensions=["country", "channel"],
    unit="EUR",
    owner="finance",
    directionality=Directionality.LOWER_IS_BAD,
    tags={"finance", "executive"},
)

profit_margin = Metric(
    id="profit_margin",
    name="Profit Margin",
    description="Contribution profit as a percentage of revenue.",
    calculation=DerivedCalculation(expression="profit / revenue"),
    dependencies=["profit", "revenue"],
    dimensions=["country", "channel"],
    unit="percent",
    format="0.0%",
    owner="finance",
    directionality=Directionality.LOWER_IS_BAD,
    detector=SeasonalZScore(lookback_periods=6, threshold=3.0),
    tags={"finance", "executive", "commercial"},
)

METRICS = [revenue, cost, profit, profit_margin]
