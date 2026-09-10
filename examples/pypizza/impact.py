from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from metric_runtime.engine import KPIEngine

try:
    from .measures import Measure
except ImportError:  # pragma: no cover - marimo local path import
    from measures import Measure


def campaign_impact_decomposition(
    engine: KPIEngine,
    start: datetime,
    end: datetime,
    filters: dict[str, str] | None = None,
    baseline_weeks: int = 6,
) -> dict[str, Any]:
    """Compare a campaign window to the average of the same prior-week windows."""
    measures = {
        "orders": Measure.ORDERS,
        "revenue": Measure.GROSS_ORDER_VALUE,
        "weekend_profit": Measure.WEEKEND_PROFIT,
        "discount_cost": Measure.DISCOUNT_COST,
        "delivery_cost": Measure.DELIVERY_COST,
        "restaurant_payout": Measure.RESTAURANT_PAYOUT,
        "payment_processing_cost": Measure.PAYMENT_PROCESSING_COST,
        "variable_cost": Measure.VARIABLE_COST,
        "threshold_band_orders": Measure.THRESHOLD_BAND_ORDERS,
        "sessions": Measure.SESSIONS,
        "promo_orders": Measure.PROMO_ORDERS,
        "new_customer_orders": Measure.NEW_CUSTOMER_ORDERS,
    }

    actual: dict[str, float] = {}
    expected: dict[str, float] = {}
    for key, measure in measures.items():
        a = engine.measure_value(measure, filters=filters, start=start, end=end)
        baselines = [
            engine.measure_value(
                measure,
                filters=filters,
                start=start - timedelta(weeks=i),
                end=end - timedelta(weeks=i),
            )
            for i in range(1, baseline_weeks + 1)
        ]
        actual[key] = a
        expected[key] = sum(baselines) / len(baselines)

    incremental = {k: actual[k] - expected[k] for k in actual}
    pct = {k: (incremental[k] / expected[k] if expected[k] else 0.0) for k in actual}

    aov_a = actual["revenue"] / actual["orders"] if actual["orders"] else 0.0
    aov_e = expected["revenue"] / expected["orders"] if expected["orders"] else 0.0
    cost_a = (
        (actual["discount_cost"] + actual["delivery_cost"] + actual["payment_processing_cost"])
        / actual["orders"]
        if actual["orders"]
        else 0.0
    )
    cost_e = (
        (
            expected["discount_cost"]
            + expected["delivery_cost"]
            + expected["payment_processing_cost"]
        )
        / expected["orders"]
        if expected["orders"]
        else 0.0
    )
    margin_a = actual["weekend_profit"] / actual["revenue"] if actual["revenue"] else 0.0
    margin_e = expected["weekend_profit"] / expected["revenue"] if expected["revenue"] else 0.0
    thresh_a = actual["threshold_band_orders"] / actual["orders"] if actual["orders"] else 0.0
    thresh_e = expected["threshold_band_orders"] / expected["orders"] if expected["orders"] else 0.0

    return {
        "actual": actual,
        "expected": expected,
        "incremental": incremental,
        "pct_change": pct,
        "average_order_value_actual": aov_a,
        "average_order_value_expected": aov_e,
        "average_order_value_pct": (aov_a - aov_e) / aov_e if aov_e else 0.0,
        "average_cost_per_order_actual": cost_a,
        "average_cost_per_order_expected": cost_e,
        "average_cost_per_order_pct": (cost_a - cost_e) / cost_e if cost_e else 0.0,
        "profit_margin_actual": margin_a,
        "profit_margin_expected": margin_e,
        "profit_margin_pct": (margin_a - margin_e) / margin_e if margin_e else 0.0,
        "threshold_concentration_actual": thresh_a,
        "threshold_concentration_expected": thresh_e,
        "threshold_concentration_pct": ((thresh_a - thresh_e) / thresh_e if thresh_e else 0.0),
        "lost_weekend_profit": max(0.0, expected["weekend_profit"] - actual["weekend_profit"]),
        # aliases for older call sites
        "gross_revenue": actual["revenue"],
        "contribution_margin": actual["weekend_profit"],
        "lost_contribution_margin": max(0.0, expected["weekend_profit"] - actual["weekend_profit"]),
    }
