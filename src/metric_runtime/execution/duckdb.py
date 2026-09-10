"""DuckDB analytical execution backend.

DuckDB is an analytical execution backend, not metric-runtime's state
database and not a streaming engine.

Domain-specific queries (basket mix, business events, …) belong in the
application/example layer — not here.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from metric_runtime.exceptions import MetricRuntimeError
from metric_runtime.models import Formula, MeasureRef, coerce_measure_ref

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_identifier(name: str) -> str:
    """Validate and double-quote a SQL identifier.

    Rejects anything that is not a plain identifier so user-controlled
    strings cannot alter generated SQL structure.
    """
    if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
        raise ValueError(
            f"Invalid SQL identifier {name!r}. "
            "Expected a name like 'orders' or 'gross_revenue'."
        )
    return f'"{name}"'


def quote_relation(name: str) -> str:
    """Validate and quote a table reference (optionally schema-qualified)."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"Invalid relation name {name!r}.")
    parts = name.split(".")
    if any(not _IDENT_RE.fullmatch(part) for part in parts):
        raise ValueError(
            f"Invalid relation name {name!r}. "
            "Expected 'table' or 'schema.table' with plain identifiers."
        )
    return ".".join(quote_identifier(part) for part in parts)


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
        self._fact_sql = quote_relation(fact_table)
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
        where, params = self._where_clause(at, filters, start, end)
        sql = f"""
            SELECT {self._metric_sql(formula)} AS value
            FROM {self._fact_sql}
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
        column = quote_identifier(coerce_measure_ref(measure))
        where, params = self._where_clause(at, filters, start, end)
        sql = f"""
            SELECT SUM({column})::DOUBLE
            FROM {self._fact_sql}
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
        rows = self.con.execute(
            f"""
            SELECT DISTINCT {columns}
            FROM {self._fact_sql}
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
            f"SELECT MAX({self._ts_sql}) FROM {self._fact_sql}"
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
            FROM {self._fact_sql}
            WHERE {self._ts_sql} = ?
            """,
            [at],
        ).fetchone()
        return int(row[0] or 0)
