"""DuckDB SQL identifier safety."""

from __future__ import annotations

from datetime import datetime

import duckdb
import pytest

from metric_runtime.execution.duckdb import DuckDBExecutor, quote_identifier, quote_relation
from metric_runtime.models import Formula


def test_quote_identifier_accepts_plain_names():
    assert quote_identifier("orders") == '"orders"'
    assert quote_relation("analytics.fact_orders") == '"analytics"."fact_orders"'


@pytest.mark.parametrize(
    "bad",
    [
        "orders; DROP TABLE x--",
        "orders)",
        "1orders",
        'ord"ers',
        "",
        "orders space",
        "schema.table;drop",
    ],
)
def test_quote_identifier_rejects_injection(bad: str):
    with pytest.raises(ValueError, match="Invalid"):
        if "." in bad:
            quote_relation(bad)
        else:
            quote_identifier(bad)


def test_executor_rejects_bad_fact_table():
    con = duckdb.connect(":memory:")
    with pytest.raises(ValueError, match="Invalid"):
        DuckDBExecutor(con, fact_table="fact; DROP TABLE secrets")


def test_executor_rejects_bad_measure_in_query():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE fact_orders (ts TIMESTAMP, orders DOUBLE, city VARCHAR)")
    con.execute(
        "INSERT INTO fact_orders VALUES (?, 10, 'Amsterdam')",
        [datetime(2026, 5, 15, 12, 0)],
    )
    executor = DuckDBExecutor(con, fact_table="fact_orders")
    with pytest.raises(ValueError, match="Invalid"):
        # Bypass Formula validation by constructing after the fact is awkward;
        # inject via measure_value which still validates the identifier.
        executor.measure_value("orders); DROP TABLE fact_orders--")


def test_executor_rejects_bad_filter_column():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE fact_orders (ts TIMESTAMP, orders DOUBLE, city VARCHAR)")
    executor = DuckDBExecutor(con, fact_table="fact_orders")
    with pytest.raises(ValueError, match="Invalid"):
        executor.metric_value(
            Formula.sum("orders"),
            at=datetime(2026, 5, 15, 12, 0),
            filters={"city; DROP TABLE fact_orders--": "Amsterdam"},
        )
