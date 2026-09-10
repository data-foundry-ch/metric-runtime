"""DuckDB analytical execution backend.

DuckDB is an analytical execution backend, not metric-runtime's state
database and not a streaming engine.

Domain-specific queries (basket mix, business events, …) belong in the
application/example layer — not here.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from metric_runtime.exceptions import MetricRuntimeError
from metric_runtime.models import Formula, MeasureRef, coerce_measure_ref


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

    def __init__(
        self,
        path_or_connection: str | Path | Any,
        *,
        fact_table: str,
        read_only: bool = True,
        valid_dimensions: set[str] | None = None,
        timestamp_column: str = "ts",
    ) -> None:
        if not fact_table:
            raise MetricRuntimeError(
                "DuckDBExecutor requires an explicit fact_table "
                "(set it in connections.yaml or pass fact_table=...)."
            )
        duckdb = _require_duckdb()
        self.fact_table = fact_table
        # None = do not restrict filter/dimension keys (catalog owns validity).
        self.valid_dimensions = valid_dimensions
        self.timestamp_column = timestamp_column
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
            return f"SUM({formula.measure})::DOUBLE"
        if formula.kind == "ratio":
            assert formula.numerator is not None and formula.denominator is not None
            return (
                f"SUM({formula.numerator})::DOUBLE / NULLIF(SUM({formula.denominator})::DOUBLE, 0)"
            )
        if formula.kind == "difference":
            assert formula.left is not None and formula.right is not None
            return f"(SUM({formula.left}) - SUM({formula.right}))::DOUBLE"
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
        allowed = valid_dimensions if valid_dimensions is not None else self.valid_dimensions
        if allowed is not None:
            bad = set(filters) - allowed
            if bad:
                raise ValueError(f"Unsupported dimensions: {sorted(bad)}")

        clauses: list[str] = []
        params: list[object] = []
        ts = self.timestamp_column

        if at is not None:
            clauses.append(f"{ts} = ?")
            params.append(at)
        if start is not None:
            clauses.append(f"{ts} >= ?")
            params.append(start)
        if end is not None:
            clauses.append(f"{ts} <= ?")
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
        measure: MeasureRef,
        *,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float:
        column = coerce_measure_ref(measure)
        where, params = self._where_clause(at, filters, start, end)
        sql = f"""
            SELECT SUM({column})::DOUBLE
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

        allowed = valid_dimensions if valid_dimensions is not None else self.valid_dimensions
        if allowed is not None:
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

    def latest_timestamp(self) -> datetime | None:
        latest = self.con.execute(
            f"SELECT MAX({self.timestamp_column}) FROM {self.fact_table}"
        ).fetchone()[0]
        if latest is None:
            return None
        if isinstance(latest, datetime):
            return latest
        return datetime.fromisoformat(str(latest))

    def row_count_at(self, at: datetime) -> int:
        row = self.con.execute(
            f"""
            SELECT COUNT(*)::INTEGER
            FROM {self.fact_table}
            WHERE {self.timestamp_column} = ?
            """,
            [at],
        ).fetchone()
        return int(row[0] or 0)
