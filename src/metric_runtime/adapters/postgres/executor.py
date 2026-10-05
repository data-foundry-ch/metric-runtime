"""Read-only Postgres analytical execution backend.

Calculates formula / measure aggregates (pushed down as single-row
``SUM`` / ``NULLIF`` queries) and ``dialect: postgres`` SQL / batch
calculations against an analytical Postgres database.

Safety:

- every session runs with ``default_transaction_read_only = on`` and every
  query runs inside an explicit ``READ ONLY`` transaction — there is no write
  API (still prefer a read-only database role);
- ``statement_timeout`` bounds each query;
- ``:name`` placeholders are bound server-side (``%(name)s``), identifiers are
  validated and double-quoted;
- the session ``TimeZone`` is UTC, so ``timestamp`` columns are interpreted as
  UTC wall-clock time (same convention as the DuckDB executor);
- the executor owns its connection: it is never shared with the (writable)
  runtime store, even when both roles use the same connection config.

Install with ``pip install "metric-runtime[postgres]"``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from metric_runtime.exceptions import MetricRuntimeError
from metric_runtime.execution.sql import bind_named_params, quote_identifier, quote_relation
from metric_runtime.identity import ensure_utc
from metric_runtime.models import Formula, MeasureRef, coerce_measure_ref

__all__ = ["PostgresExecutor"]


def _require_psycopg() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise MetricRuntimeError(
            'Postgres source support requires: pip install "metric-runtime[postgres]"'
        ) from exc
    return psycopg


def _escape_percent(query: str) -> str:
    # psycopg treats '%' as a placeholder marker once parameters are passed.
    return query.replace("%", "%%")


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else ensure_utc(value)
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else ensure_utc(parsed)


class PostgresExecutor:
    """Evaluate metric formulas and Postgres SQL against an analytical database."""

    sql_dialects = frozenset({"postgres"})

    def __init__(
        self,
        conninfo: str = "",
        *,
        fact_table: str | None = None,
        timestamp_column: str = "ts",
        valid_dimensions: set[str] | None = None,
        statement_timeout_ms: int | None = 30_000,
        search_path: str | None = None,
        connect_kwargs: dict[str, Any] | None = None,
        connection: Any | None = None,
    ) -> None:
        self.search_path = search_path
        self._search_path_sql = quote_identifier(search_path) if search_path else None
        self.fact_table = fact_table
        self._fact_sql = quote_relation(fact_table) if fact_table else None
        self.timestamp_column = timestamp_column
        self._ts_sql = quote_identifier(timestamp_column)
        self.valid_dimensions = valid_dimensions
        self.statement_timeout_ms = statement_timeout_ms
        self._conninfo = conninfo
        self._connect_kwargs = dict(connect_kwargs or {})
        self._conn = connection
        self._owns_connection = connection is None
        if connection is not None:
            self._configure(connection)

    # --- connection ---------------------------------------------------------------------

    def _configure(self, conn: Any) -> None:
        conn.execute("SET default_transaction_read_only = on")
        conn.execute("SET TIME ZONE 'UTC'")
        if self._search_path_sql is not None:
            conn.execute(f"SET search_path TO {self._search_path_sql}")
        if self.statement_timeout_ms is not None:
            conn.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")

    def _connection(self) -> Any:
        conn = self._conn
        if conn is not None and not (conn.closed or conn.broken):
            return conn
        if not self._owns_connection:
            raise MetricRuntimeError("PostgresExecutor connection is closed")
        psycopg = _require_psycopg()
        conn = psycopg.connect(self._conninfo, autocommit=True, **self._connect_kwargs)
        self._configure(conn)
        self._conn = conn
        return conn

    @contextmanager
    def _read_only(self) -> Iterator[Any]:
        conn = self._connection()
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            yield conn

    def _fetch(self, sql: str, params: dict[str, Any]) -> tuple[list[str], list[tuple]]:
        with self._read_only() as conn:
            cursor = conn.execute(sql, params)
            columns = [col.name for col in (cursor.description or [])]
            return columns, cursor.fetchall()

    def close(self) -> None:
        if self._owns_connection and self._conn is not None:
            self._conn.close()
        self._conn = None

    def __enter__(self) -> PostgresExecutor:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def ping(self) -> bool:
        """Connectivity check used by ``metric-runtime connections test``."""
        self._fetch("SELECT 1", {})
        return True

    # --- formula / measure aggregates ---------------------------------------------------------

    def _require_fact_table_sql(self) -> str:
        if self._fact_sql is None:
            raise MetricRuntimeError(
                "PostgresExecutor requires fact_table for formula/measure evaluation; "
                "omit it only for SQL/batch-only sessions."
            )
        return self._fact_sql

    @staticmethod
    def _metric_sql(formula: Formula) -> str:
        if formula.kind == "sum":
            assert formula.measure is not None
            return f"SUM({quote_identifier(formula.measure)})::double precision"
        if formula.kind == "ratio":
            assert formula.numerator is not None and formula.denominator is not None
            num = quote_identifier(formula.numerator)
            den = quote_identifier(formula.denominator)
            return f"SUM({num})::double precision / NULLIF(SUM({den})::double precision, 0)"
        if formula.kind == "difference":
            assert formula.left is not None and formula.right is not None
            left = quote_identifier(formula.left)
            right = quote_identifier(formula.right)
            return f"(SUM({left}) - SUM({right}))::double precision"
        raise ValueError(f"Unknown formula kind: {formula.kind}")

    def _where_clause(
        self,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        valid_dimensions: set[str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        filters = filters or {}
        allowed = valid_dimensions if valid_dimensions is not None else self.valid_dimensions
        if allowed is not None:
            bad = set(filters) - allowed
            if bad:
                raise ValueError(f"Unsupported dimensions: {sorted(bad)}")
        clauses: list[str] = []
        params: dict[str, Any] = {}
        ts_sql = self._ts_sql
        for op, name, value in (("=", "at", at), (">=", "start", start), ("<=", "end", end)):
            if value is not None:
                clauses.append(f"{ts_sql} {op} %({name})s")
                params[name] = ensure_utc(value)
        for i, (column, filter_value) in enumerate(filters.items()):
            clauses.append(f"{quote_identifier(column)} = %(f{i})s")
            params[f"f{i}"] = filter_value
        return " AND ".join(clauses) or "TRUE", params

    def _aggregate(self, select: str, where: str, params: dict[str, Any]) -> float:
        sql = f"SELECT {select} AS value FROM {self._require_fact_table_sql()} WHERE {where}"
        _, rows = self._fetch(sql, params)
        value = rows[0][0] if rows else None
        return float(value or 0.0)

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
        return self._aggregate(self._metric_sql(formula), where, params)

    def measure_value(
        self,
        measure: MeasureRef,
        *,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float:
        column = quote_identifier(coerce_measure_ref(measure))
        where, params = self._where_clause(at, filters, start, end)
        return self._aggregate(f"SUM({column})::double precision", where, params)

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
        columns = ", ".join(quote_identifier(d) for d in dimensions)
        sql = (
            f"SELECT DISTINCT {columns} FROM {self._require_fact_table_sql()} "
            f"WHERE {where} ORDER BY {columns}"
        )
        _, rows = self._fetch(sql, params)
        base = dict(filters or {})
        return [
            {**base, **dict(zip(dimensions, (str(v) for v in row), strict=False))} for row in rows
        ]

    def latest_timestamp(self) -> datetime | None:
        _, rows = self._fetch(
            f"SELECT MAX({self._ts_sql}) FROM {self._require_fact_table_sql()}", {}
        )
        latest = rows[0][0] if rows else None
        return None if latest is None else _as_utc(latest)

    def row_count_at(self, at: datetime) -> int:
        _, rows = self._fetch(
            f"SELECT COUNT(*) FROM {self._require_fact_table_sql()} WHERE {self._ts_sql} = %(at)s",
            {"at": ensure_utc(at)},
        )
        return int(rows[0][0] or 0)

    # --- SQL calculation interface (scalar / named-row) -----------------------------------------

    def _assert_sql_dialect(self, dialect: str | None) -> None:
        if dialect is not None and dialect not in self.sql_dialects:
            raise MetricRuntimeError(
                f"PostgresExecutor does not support dialect {dialect!r} "
                f"(supports {sorted(self.sql_dialects)}). "
                "Metric Runtime does not transpile SQL across warehouses."
            )

    def _run_sql(
        self, query: str, parameters: dict[str, Any] | None
    ) -> tuple[list[str], list[tuple]]:
        sql, params = bind_named_params(
            _escape_percent(query),
            parameters or {},
            placeholder=r"%(\1)s",
            convert=lambda v: ensure_utc(v) if isinstance(v, datetime) else v,
        )
        try:
            return self._fetch(sql, params)
        except MetricRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface driver errors uniformly
            raise MetricRuntimeError(f"SQL execution failed: {exc}") from exc

    def execute_scalar(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        dialect: str | None = None,
        value_column: str = "value",
    ) -> float | None:
        """Execute parameterized SQL expecting at most one row with ``value_column``."""
        self._assert_sql_dialect(dialect)
        columns, rows = self._run_sql(query, parameters)
        if not rows:
            return None
        if len(rows) > 1:
            raise MetricRuntimeError(f"SQL calculation expected exactly one row, got {len(rows)}")
        if value_column not in columns:
            raise MetricRuntimeError(
                f"SQL calculation result missing column {value_column!r}; got columns {columns}"
            )
        raw = rows[0][columns.index(value_column)]
        return None if raw is None else float(raw)

    def execute_named_row(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        dialect: str | None = None,
    ) -> dict[str, Any]:
        """Execute parameterized SQL expecting exactly one row of named columns."""
        self._assert_sql_dialect(dialect)
        columns, rows = self._run_sql(query, parameters)
        if not rows:
            raise MetricRuntimeError("SQL batch source returned zero rows")
        if len(rows) > 1:
            raise MetricRuntimeError(f"SQL batch source expected exactly one row, got {len(rows)}")
        return dict(zip(columns, rows[0], strict=False))
