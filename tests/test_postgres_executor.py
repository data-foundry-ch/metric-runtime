"""Read-only PostgresExecutor (skipped without METRIC_RUNTIME_TEST_POSTGRES_DSN)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from _runtime_helpers import ts
from metric_runtime import KPIEngine, Metric, MetricCatalog, SqlCalculation
from metric_runtime.calculations import BatchRegistry, SqlBatchSource
from metric_runtime.exceptions import MetricRuntimeError
from metric_runtime.models import Formula

TICK = ts(2026, 5, 15, 12, 15)
WEEKS = 6


@pytest.fixture
def analytics(pg_dsn) -> Iterator[str]:
    """Seed a unique analytics schema: facts(ts, city, revenue, profit, orders)."""
    import psycopg

    schema = f"an_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(
            f'CREATE TABLE "{schema}".facts ('
            " ts timestamp NOT NULL, city text NOT NULL,"
            " revenue double precision, profit double precision, orders integer,"
            " empty_den double precision)"
        )
        rows = []
        for week in range(WEEKS + 1):
            at = (TICK - timedelta(weeks=week)).replace(tzinfo=None)
            for city, revenue, profit, orders in (("ams", 100.0, 30.0, 10), ("rtm", 50.0, 10.0, 5)):
                rows.append((at, city, revenue + week, profit, orders, 0.0))
        with conn.cursor() as cur:
            cur.executemany(f'INSERT INTO "{schema}".facts VALUES (%s, %s, %s, %s, %s, %s)', rows)
    try:
        yield schema
    finally:
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def executor(pg_dsn, analytics):
    from metric_runtime.execution import PostgresExecutor

    ex = PostgresExecutor(pg_dsn, fact_table=f"{analytics}.facts", statement_timeout_ms=2000)
    try:
        yield ex
    finally:
        ex.close()


def test_formula_aggregates_are_pushed_down(executor):
    assert executor.metric_value(Formula.sum("revenue"), at=TICK) == 150.0
    assert executor.metric_value(Formula.ratio("profit", "revenue"), at=TICK) == pytest.approx(
        40.0 / 150.0
    )
    assert executor.metric_value(Formula.difference("revenue", "profit"), at=TICK) == 110.0
    assert executor.metric_value(Formula.sum("revenue"), at=TICK, filters={"city": "ams"}) == 100.0
    # NULLIF guards division by zero; NULL maps to 0.0 like the DuckDB executor.
    assert executor.metric_value(Formula.ratio("profit", "empty_den"), at=TICK) == 0.0
    assert executor.measure_value("orders", at=TICK) == 15.0
    window = executor.metric_value(Formula.sum("orders"), start=TICK - timedelta(weeks=1), end=TICK)
    assert window == 30.0


def test_introspection_helpers(executor):
    assert executor.latest_timestamp() == TICK
    assert executor.row_count_at(TICK) == 2
    assert executor.distinct_groups(("city",), at=TICK) == [{"city": "ams"}, {"city": "rtm"}]
    assert executor.ping() is True


def test_identifiers_are_validated(executor):
    with pytest.raises(ValueError):
        executor.metric_value(Formula.sum("revenue"), at=TICK, filters={"city; drop": "x"})
    from metric_runtime.execution import PostgresExecutor

    with pytest.raises(ValueError):
        PostgresExecutor("", fact_table="facts; DROP TABLE x")


def test_named_parameters_bind_server_side(executor, analytics):
    value = executor.execute_scalar(
        f"SELECT SUM(revenue) AS value FROM {analytics}.facts "
        "WHERE ts = :effective_at AND city LIKE 'a%' AND orders >= :min_orders::int",
        {"effective_at": TICK, "min_orders": 1, "unused": "ignored"},
        dialect="postgres",
    )
    assert value == 100.0
    with pytest.raises(MetricRuntimeError, match="Missing SQL parameter"):
        executor.execute_scalar("SELECT :nope AS value", {})
    # Values are bound, never interpolated.
    injected = executor.execute_scalar(
        f"SELECT COUNT(*) AS value FROM {analytics}.facts WHERE city = :city",
        {"city": "ams' OR '1'='1"},
    )
    assert injected == 0.0


def test_single_row_semantics(executor, analytics):
    assert (
        executor.execute_scalar(f"SELECT revenue AS value FROM {analytics}.facts WHERE false")
        is None
    )
    with pytest.raises(MetricRuntimeError, match="exactly one row"):
        executor.execute_scalar(f"SELECT revenue AS value FROM {analytics}.facts")
    with pytest.raises(MetricRuntimeError, match="missing column"):
        executor.execute_scalar("SELECT 1 AS other")
    row = executor.execute_named_row("SELECT 1.5 AS a, NULL::float AS b")
    assert row == {"a": pytest.approx(1.5), "b": None}
    with pytest.raises(MetricRuntimeError, match="zero rows"):
        executor.execute_named_row("SELECT 1 AS a WHERE false")


def test_read_only_is_enforced(executor, analytics):
    with pytest.raises(MetricRuntimeError, match="read-only"):
        executor.execute_scalar(
            f"INSERT INTO {analytics}.facts (ts, city) VALUES (now(), 'x') RETURNING 1 AS value"
        )
    with pytest.raises(MetricRuntimeError):
        executor.execute_scalar(
            "SET default_transaction_read_only = off; "
            f"DELETE FROM {analytics}.facts RETURNING 1 AS value"
        )
    assert executor.row_count_at(TICK) == 2


def test_statement_timeout(pg_dsn):
    from metric_runtime.execution import PostgresExecutor

    ex = PostgresExecutor(pg_dsn, statement_timeout_ms=200)
    try:
        with pytest.raises(MetricRuntimeError, match="statement timeout|canceling"):
            ex.execute_scalar("SELECT pg_sleep(2) AS value")
        assert ex.execute_scalar("SELECT 1 AS value") == 1.0  # session still usable
    finally:
        ex.close()


def test_other_dialects_are_rejected(executor):
    with pytest.raises(MetricRuntimeError, match="does not transpile"):
        executor.execute_scalar("SELECT 1 AS value", dialect="duckdb")


def test_reconnects_after_connection_loss(executor):
    assert executor.ping()
    executor._conn.close()
    assert executor.execute_scalar("SELECT 2 AS value") == 2.0


def _catalog(schema: str) -> MetricCatalog:
    return MetricCatalog(
        [
            Metric(id="revenue", name="Revenue", formula=Formula.sum("revenue")),
            Metric(
                id="margin",
                name="Margin",
                formula=Formula.ratio("profit", "revenue"),
                dependencies=("revenue",),
            ),
            Metric(
                id="orders_sql",
                name="Orders (SQL)",
                calculation=SqlCalculation(
                    dialect="postgres",
                    query=f"SELECT SUM(orders)::float AS value FROM {schema}.facts "
                    "WHERE ts = :effective_at",
                ),
            ),
        ]
    )


def test_engine_session_uses_executor_dialects(executor, analytics):
    # Before the fix, sessions only accepted dialect "duckdb".
    engine = KPIEngine(_catalog(analytics), executor=executor)
    session = engine.new_evaluation_session()
    assert session.sql_dialects == frozenset({"postgres"})
    assert engine.metric_value("orders_sql", TICK) == 15.0
    status = engine.evaluate("orders_sql", TICK)
    assert status.value == 15.0 and status.baseline_mean == 15.0


def test_sql_batch_source_on_postgres(executor, analytics):
    from metric_runtime import BatchCalculation

    registry = BatchRegistry()
    registry.register(
        "city_totals",
        SqlBatchSource(
            "city_totals",
            dialect="postgres",
            query=f"SELECT SUM(revenue) FILTER (WHERE city = 'ams') AS ams, "
            f"SUM(revenue) FILTER (WHERE city = 'rtm') AS rtm "
            f"FROM {analytics}.facts WHERE ts = :effective_at",
        ),
    )
    catalog = MetricCatalog(
        [
            Metric(id="ams", name="AMS", calculation=BatchCalculation(source="city_totals")),
            Metric(id="rtm", name="RTM", calculation=BatchCalculation(source="city_totals")),
        ]
    )
    engine = KPIEngine(catalog, executor=executor, batch_registry=registry)
    assert engine.metric_value("ams", TICK) == 100.0
    assert engine.metric_value("rtm", TICK) == 50.0


def test_run_once_with_both_roles_on_one_server(
    tmp_path: Path, monkeypatch, capsys, pg_dsn, pg_schema, analytics
):
    """End-to-end: Postgres source + Postgres runtime store, separate schemas."""
    from metric_runtime.cli import main

    module = f"pgcat_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{module}.py").write_text(
        "from metric_runtime import Metric, MetricCatalog, SqlCalculation\n"
        "from metric_runtime.models import Formula\n"
        "catalog = MetricCatalog([\n"
        "    Metric(id='revenue', name='Revenue', formula=Formula.sum('revenue')),\n"
        "    Metric(id='orders_sql', name='Orders', calculation=SqlCalculation(\n"
        "        dialect='postgres',\n"
        f"        query='SELECT SUM(orders)::float AS value FROM {analytics}.facts "
        "WHERE ts = :effective_at')),\n"
        "])\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    project = tmp_path / "metric-runtime.yaml"
    project.write_text(
        f"catalog:\n  entrypoint: {module}:catalog\n"
        "runtime:\n  default_profile: production\n  evaluation_interval: 15m\n",
        encoding="utf-8",
    )
    conns = tmp_path / "connections.yaml"
    conns.write_text(
        "connections:\n"
        f"  warehouse:\n    type: postgres\n    dsn: {pg_dsn}\n"
        f"    fact_table: {analytics}.facts\n    statement_timeout: 10s\n"
        f"  runtime_db:\n    type: postgres\n    dsn: {pg_dsn}\n    schema: {pg_schema}\n"
        "profiles:\n  production:\n    metric_source: warehouse\n    runtime_store: runtime_db\n"
        "    notifier:\n      type: none\n",
        encoding="utf-8",
    )
    base = ["--project-config", str(project), "--connections", str(conns), "--log-level", "ERROR"]

    assert main(["validate", *base]) == 0
    capsys.readouterr()
    assert main(["store", "migrate", *base]) == 0
    capsys.readouterr()
    assert main(["run", "--once", "--now", "2026-05-15T12:30:00Z", "--json", *base]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert {(o["metric"], o["status"]) for o in report["outcomes"]} == {
        ("revenue", "evaluated"),
        ("orders_sql", "evaluated"),
    }
    assert main(["run", "--once", "--now", "2026-05-15T12:30:00Z", "--json", *base]) == 0
    assert json.loads(capsys.readouterr().out)["outcomes"] == []

    import psycopg

    with psycopg.connect(pg_dsn) as conn:
        runtime_tables = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
            (analytics,),
        ).fetchone()[0]
        evaluations = conn.execute(f'SELECT count(*) FROM "{pg_schema}".evaluations').fetchone()[0]
    assert runtime_tables == 1  # only the seeded facts table lives in the source schema
    assert evaluations == 2


def test_same_schema_for_both_roles_is_rejected(tmp_path: Path, capsys, pg_dsn):
    from metric_runtime.cli import main

    conns = tmp_path / "connections.yaml"
    conns.write_text(
        "connections:\n"
        f"  warehouse:\n    type: postgres\n    dsn: {pg_dsn}\n    fact_table: analytics.facts\n"
        f"  runtime_db:\n    type: postgres\n    dsn: {pg_dsn}\n    schema: analytics\n"
        "profiles:\n  production:\n    metric_source: warehouse\n    runtime_store: runtime_db\n",
        encoding="utf-8",
    )
    project = tmp_path / "metric-runtime.yaml"
    project.write_text("runtime:\n  default_profile: production\n", encoding="utf-8")
    base = ["--project-config", str(project), "--connections", str(conns)]
    assert main(["validate", *base]) == 1
    assert "same database and schema 'analytics'" in capsys.readouterr().err
    assert main(["run", "--once", *base]) == 1
