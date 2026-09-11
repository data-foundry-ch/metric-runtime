"""PyPizza-specific analytical queries (example layer)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from metric_runtime.engine import KPIEngine


def nearby_events(
    engine: KPIEngine,
    at: datetime,
    filters: dict[str, str] | None = None,
    hours: int = 48,
    events_table: str = "business_events",
) -> list[dict[str, object]]:
    if engine.con is None:
        return []
    filters = filters or {}
    start = at - timedelta(hours=hours)
    end = at + timedelta(hours=hours)
    if start.tzinfo is not None:
        start = start.astimezone(UTC).replace(tzinfo=None)
        end = end.astimezone(UTC).replace(tzinfo=None)

    clauses = ["event_ts BETWEEN ? AND ?"]
    params: list[object] = [start, end]
    if "city" in filters:
        clauses.append("(scope_city IS NULL OR scope_city = ?)")
        params.append(filters["city"])

    sql = f"""
        SELECT
            event_ts,
            event_type,
            title,
            detail,
            scope_city,
            scope_meal_period,
            relevance_tags
        FROM {events_table}
        WHERE {" AND ".join(clauses)}
        ORDER BY event_ts
    """
    result = []
    for row in engine.con.execute(sql, params).fetchall():
        result.append(
            {
                "event_ts": row[0],
                "event_type": row[1],
                "title": row[2],
                "detail": row[3],
                "scope_city": row[4],
                "scope_meal_period": row[5],
                "relevance_tags": row[6],
            }
        )
    return result


def basket_distribution(
    engine: KPIEngine,
    start: datetime,
    end: datetime,
    filters: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """Basket-band mix for the Great Lunch cliff visualization."""
    if engine.executor is None:
        raise RuntimeError("basket_distribution requires a DuckDB executor")
    where, params = engine.executor._where_clause(  # noqa: SLF001
        filters=filters, start=start, end=end
    )
    rows = engine.con.execute(
        f"""
        SELECT
            basket_band,
            SUM(orders)::DOUBLE AS orders,
            SUM(gross_order_value_eur)::DOUBLE AS gmv,
            SUM(discount_cost_eur)::DOUBLE AS discount,
            SUM(weekend_profit_eur)::DOUBLE AS margin
        FROM {engine.fact_table}
        WHERE {where}
        GROUP BY basket_band
        ORDER BY
            CASE basket_band
                WHEN '<20' THEN 1
                WHEN '20-24.99' THEN 2
                WHEN '25-29.99' THEN 3
                WHEN '30-39.99' THEN 4
                WHEN '40+' THEN 5
                ELSE 6
            END
        """,
        params,
    ).fetchall()
    return [
        {
            "basket_band": r[0],
            "orders": float(r[1] or 0),
            "gmv": float(r[2] or 0),
            "discount": float(r[3] or 0),
            "margin": float(r[4] or 0),
        }
        for r in rows
    ]
