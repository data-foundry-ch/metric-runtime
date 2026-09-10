"""DuckDB analytical execution backend.

DuckDB is an analytical execution backend, not metric-runtime's state
database and not a streaming engine.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from metric_runtime.exceptions import MetricRuntimeError
from metric_runtime.models import Formula, Measure


def _require_duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise MetricRuntimeError(
            'DuckDB support requires the optional dependency: pip install "metric-runtime[duckdb]"'
        ) from exc
    return duckdb


class DuckDBExecutor:
    """Evaluate KPI formulas against a DuckDB database or connection."""

    DEFAULT_VALID_DIMENSIONS = {
        "city",
        "meal_period",
        "customer_type",
        "basket_band",
        "channel",
        "restaurant_category",
        "device",
        "campaign",
        "day_of_week",
    }

    def __init__(
        self,
        path_or_connection: str | Path | Any,
        *,
        fact_table: str = "pypizza_halfhourly",
        read_only: bool = True,
        valid_dimensions: set[str] | None = None,
    ) -> None:
        duckdb = _require_duckdb()
        self.fact_table = fact_table
        self.valid_dimensions = valid_dimensions or set(self.DEFAULT_VALID_DIMENSIONS)
        self._owns_connection = False

        if hasattr(path_or_connection, "execute"):
            self.con = path_or_connection
        else:
            self.con = duckdb.connect(str(path_or_connection), read_only=read_only)
            self._owns_connection = True

    def close(self) -> None:
        if self._owns_connection and self.con is not None:
            close = getattr(self.con, "close", None)
            if callable(close):
                close()
            self.con = None

    def __enter__(self) -> DuckDBExecutor:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def ping(self) -> bool:
        """Connectivity check used by ``metric-runtime connections test``."""
        self.con.execute("SELECT 1").fetchone()
        return True

    def _metric_sql(self, formula: Formula) -> str:
        if formula.kind == "sum":
            assert formula.measure is not None
            return f"SUM({formula.measure.value})::DOUBLE"
        if formula.kind == "ratio":
            assert formula.numerator is not None and formula.denominator is not None
            n = formula.numerator.value
            d = formula.denominator.value
            return f"SUM({n})::DOUBLE / NULLIF(SUM({d})::DOUBLE, 0)"
        if formula.kind == "difference":
            assert formula.left is not None and formula.right is not None
            return f"(SUM({formula.left.value}) - SUM({formula.right.value}))::DOUBLE"
        raise ValueError(f"Unknown formula kind: {formula.kind}")

    def _where_clause(
        self,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        valid_dimensions: set[str] | None = None,
    ) -> tuple[str, list[object]]:
        filters = filters or {}
        allowed = valid_dimensions or self.valid_dimensions
        bad = set(filters) - allowed
        if bad:
            raise ValueError(f"Unsupported dimensions: {sorted(bad)}")

        clauses: list[str] = []
        params: list[object] = []

        if at is not None:
            clauses.append("ts = ?")
            params.append(at)
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts <= ?")
            params.append(end)
        if not clauses:
            clauses.append("1 = 1")

        for column, value in filters.items():
            clauses.append(f"{column} = ?")
            params.append(value)

        return " AND ".join(clauses), params

    def metric_value(
        self,
        formula: Formula,
        *,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float:
        where, params = self._where_clause(at, filters, start, end)
        sql = f"""
            SELECT {self._metric_sql(formula)} AS value
            FROM {self.fact_table}
            WHERE {where}
        """
        value = self.con.execute(sql, params).fetchone()[0]
        return float(value or 0.0)

    def measure_value(
        self,
        measure: Measure,
        *,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float:
        where, params = self._where_clause(at, filters, start, end)
        sql = f"""
            SELECT SUM({measure.value})::DOUBLE
            FROM {self.fact_table}
            WHERE {where}
        """
        value = self.con.execute(sql, params).fetchone()[0]
        return float(value or 0.0)

    def distinct_groups(
        self,
        dimensions: tuple[str, ...],
        *,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        valid_dimensions: set[str] | None = None,
    ) -> list[dict[str, str]]:
        if not dimensions:
            return [dict(filters or {})]

        allowed = valid_dimensions or self.valid_dimensions
        bad = set(dimensions) - allowed
        if bad:
            raise ValueError(f"Unsupported dimensions: {sorted(bad)}")

        where, params = self._where_clause(at, filters, start, end, valid_dimensions=allowed)
        columns = ", ".join(dimensions)
        rows = self.con.execute(
            f"""
            SELECT DISTINCT {columns}
            FROM {self.fact_table}
            WHERE {where}
            ORDER BY {columns}
            """,
            params,
        ).fetchall()

        base = dict(filters or {})
        return [
            {**base, **dict(zip(dimensions, (str(v) for v in row), strict=False))} for row in rows
        ]

    def nearby_events(
        self,
        at: datetime,
        filters: dict[str, str] | None = None,
        hours: int = 48,
        events_table: str = "business_events",
    ) -> list[dict[str, object]]:
        from datetime import timedelta

        filters = filters or {}
        start = at - timedelta(hours=hours)
        end = at + timedelta(hours=hours)

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
        for row in self.con.execute(sql, params).fetchall():
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
        self,
        start: datetime,
        end: datetime,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        where, params = self._where_clause(filters=filters, start=start, end=end)
        rows = self.con.execute(
            f"""
            SELECT
                basket_band,
                SUM(orders)::DOUBLE AS orders,
                SUM(gross_order_value_eur)::DOUBLE AS gmv,
                SUM(discount_cost_eur)::DOUBLE AS discount,
                SUM(weekend_profit_eur)::DOUBLE AS margin
            FROM {self.fact_table}
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

    def latest_timestamp(self) -> datetime | None:
        latest = self.con.execute(f"SELECT MAX(ts) FROM {self.fact_table}").fetchone()[0]
        if latest is None:
            return None
        if isinstance(latest, datetime):
            return latest
        return datetime.fromisoformat(str(latest))

    def row_stats_at(self, at: datetime) -> tuple[int, float]:
        row = self.con.execute(
            f"""
            SELECT
                COUNT(*)::INTEGER,
                AVG(CASE WHEN orders IS NULL THEN 1.0 ELSE 0.0 END)::DOUBLE
            FROM {self.fact_table}
            WHERE ts = ?
            """,
            [at],
        ).fetchone()
        return int(row[0] or 0), float(row[1] or 0.0)
