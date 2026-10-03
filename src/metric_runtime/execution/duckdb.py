"""DuckDB analytical execution backend.

DuckDB is an analytical execution backend, not metric-runtime's state
database and not a streaming engine.

Domain-specific queries (basket mix, business events, …) belong in the
application/example layer — not here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from metric_runtime.exceptions import MetricRuntimeError
from metric_runtime.execution.sql import bind_named_params, quote_identifier, quote_relation
from metric_runtime.identity import ensure_utc
from metric_runtime.models import Formula, MeasureRef, coerce_measure_ref

__all__ = ["DuckDBExecutor", "quote_identifier", "quote_relation"]


def _bind_timestamp(value: datetime) -> datetime:
    """Bind core timezone-aware timestamps to DuckDB TIMESTAMP (UTC wall clock)."""
    return ensure_utc(value).replace(tzinfo=None)


def _from_duckdb_timestamp(value: datetime | str) -> datetime:
    """Interpret DuckDB TIMESTAMP values as UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return ensure_utc(value)
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return ensure_utc(parsed)


def _require_duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise MetricRuntimeError(
            'DuckDB support requires the optional dependency: pip install "metric-runtime[duckdb]"'
        ) from exc
    return duckdb


class DuckDBExecutor:
    """Evaluate KPI formulas and SQL against a DuckDB database or connection.

    ``fact_table`` is required for formula/measure aggregation against a
    designated fact relation. Omit it for SQL/batch-only sessions.
    """

    def __init__(
        self,
        path_or_connection: str | Path | Any,
        *,
        fact_table: str | None = None,
        read_only: bool = True,
        valid_dimensions: set[str] | None = None,
        timestamp_column: str = "ts",
    ) -> None:
        duckdb = _require_duckdb()
        self.fact_table = fact_table
        self._fact_sql = quote_relation(fact_table) if fact_table else None
        self._ts_sql = quote_identifier(timestamp_column)
        # None = do not restrict filter/dimension keys (catalog owns validity).
        self.valid_dimensions = valid_dimensions
        self.timestamp_column = timestamp_column
        self._owns_connection = False

        if hasattr(path_or_connection, "execute"):
            self.con = path_or_connection
        else:
            self.con = duckdb.connect(str(path_or_connection), read_only=read_only)
            self._owns_connection = True

    def _require_fact_table_sql(self) -> str:
        if self._fact_sql is None:
            raise MetricRuntimeError(
                "DuckDBExecutor requires fact_table for formula/measure evaluation; "
                "omit it only for SQL/batch-only sessions."
            )
        return self._fact_sql

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
            measure = quote_identifier(formula.measure)
            return f"SUM({measure})::DOUBLE"
        if formula.kind == "ratio":
            assert formula.numerator is not None and formula.denominator is not None
            num = quote_identifier(formula.numerator)
            den = quote_identifier(formula.denominator)
            return f"SUM({num})::DOUBLE / NULLIF(SUM({den})::DOUBLE, 0)"
        if formula.kind == "difference":
            assert formula.left is not None and formula.right is not None
            left = quote_identifier(formula.left)
            right = quote_identifier(formula.right)
            return f"(SUM({left}) - SUM({right}))::DOUBLE"
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
        ts = self._ts_sql

        if at is not None:
            clauses.append(f"{ts} = ?")
            params.append(_bind_timestamp(at))
        if start is not None:
            clauses.append(f"{ts} >= ?")
            params.append(_bind_timestamp(start))
        if end is not None:
            clauses.append(f"{ts} <= ?")
            params.append(_bind_timestamp(end))
        if not clauses:
            clauses.append("1 = 1")

        for column, value in filters.items():
            clauses.append(f"{quote_identifier(column)} = ?")
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
        fact_sql = self._require_fact_table_sql()
        where, params = self._where_clause(at, filters, start, end)
        sql = f"""
            SELECT {self._metric_sql(formula)} AS value
            FROM {fact_sql}
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
        fact_sql = self._require_fact_table_sql()
        column = quote_identifier(coerce_measure_ref(measure))
        where, params = self._where_clause(at, filters, start, end)
        sql = f"""
            SELECT SUM({column})::DOUBLE
            FROM {fact_sql}
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
        quoted = [quote_identifier(d) for d in dimensions]
        columns = ", ".join(quoted)
        fact_sql = self._require_fact_table_sql()
        rows = self.con.execute(
            f"""
            SELECT DISTINCT {columns}
            FROM {fact_sql}
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
        fact_sql = self._require_fact_table_sql()
        latest = self.con.execute(f"SELECT MAX({self._ts_sql}) FROM {fact_sql}").fetchone()[0]
        if latest is None:
            return None
        return _from_duckdb_timestamp(latest)

    def row_count_at(self, at: datetime) -> int:
        fact_sql = self._require_fact_table_sql()
        row = self.con.execute(
            f"""
            SELECT COUNT(*)::INTEGER
            FROM {fact_sql}
            WHERE {self._ts_sql} = ?
            """,
            [_bind_timestamp(at)],
        ).fetchone()
        return int(row[0] or 0)

    # --- SQL calculation interface (scalar / named-row) ---

    sql_dialects = frozenset({"duckdb"})

    def execute_scalar(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        dialect: str | None = None,
        value_column: str = "value",
    ) -> float | None:
        """Execute parameterized SQL expecting exactly one row with ``value_column``."""
        self._assert_sql_dialect(dialect)
        sql, params = _normalize_sql_params(query, parameters or {})
        try:
            cursor = self.con.execute(sql, params)
        except Exception as exc:  # noqa: BLE001
            raise MetricRuntimeError(f"SQL execution failed: {exc}") from exc
        description = cursor.description or []
        columns = [col[0] for col in description]
        rows = cursor.fetchall()
        if len(rows) == 0:
            return None
        if len(rows) > 1:
            raise MetricRuntimeError(f"SQL calculation expected exactly one row, got {len(rows)}")
        if value_column not in columns:
            raise MetricRuntimeError(
                f"SQL calculation result missing column {value_column!r}; got columns {columns}"
            )
        idx = columns.index(value_column)
        raw = rows[0][idx]
        if raw is None:
            return None
        return float(raw)

    def execute_named_row(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        dialect: str | None = None,
    ) -> dict[str, Any]:
        """Execute parameterized SQL expecting exactly one row of named columns."""
        self._assert_sql_dialect(dialect)
        sql, params = _normalize_sql_params(query, parameters or {})
        try:
            cursor = self.con.execute(sql, params)
        except Exception as exc:  # noqa: BLE001
            raise MetricRuntimeError(f"SQL execution failed: {exc}") from exc
        description = cursor.description or []
        columns = [col[0] for col in description]
        rows = cursor.fetchall()
        if len(rows) == 0:
            raise MetricRuntimeError("SQL batch source returned zero rows")
        if len(rows) > 1:
            raise MetricRuntimeError(f"SQL batch source expected exactly one row, got {len(rows)}")
        return dict(zip(columns, rows[0], strict=False))

    def _assert_sql_dialect(self, dialect: str | None) -> None:
        if dialect is None:
            return
        if dialect not in self.sql_dialects:
            raise MetricRuntimeError(
                f"DuckDBExecutor does not support dialect {dialect!r} "
                f"(supports {sorted(self.sql_dialects)}). "
                "Metric Runtime does not transpile SQL across warehouses."
            )


def _normalize_sql_params(
    query: str,
    parameters: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Translate ``:name`` placeholders to DuckDB ``$name`` and bind values.

    Only parameters referenced by the query are bound — DuckDB rejects excess
    named parameters.
    """
    return bind_named_params(
        query,
        parameters,
        placeholder=r"$\1",
        convert=lambda v: _bind_timestamp(v) if isinstance(v, datetime) else v,
    )
