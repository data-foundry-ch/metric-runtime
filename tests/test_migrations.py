"""Numbered runtime-store migrations (Postgres; skipped without a test DSN)."""

from __future__ import annotations

import threading

import pytest

from metric_runtime.exceptions import RuntimeStoreNotMigratedError
from metric_runtime.stores.migrations import (
    Migration,
    MigrationChecksumError,
    applied_migrations,
    load_migrations,
    migrate,
    pending_migrations,
)


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def test_packaged_migrations_are_numbered_and_ordered():
    migrations = load_migrations()
    assert migrations[0].filename == "001_initial.sql"
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)
    assert "\r\n" not in migrations[0].sql


def test_fresh_apply_then_idempotent_rerun(pg_dsn, pg_schema):
    with _connect(pg_dsn) as conn:
        assert [m.version for m in pending_migrations(conn, pg_schema)] == [1]
        applied = migrate(conn, pg_schema)
        assert [m.version for m in applied] == [1]
        assert migrate(conn, pg_schema) == []
        assert pending_migrations(conn, pg_schema) == []
        assert set(applied_migrations(conn, pg_schema)) == {1}


def test_store_refuses_to_run_with_pending_migrations(pg_dsn, pg_schema):
    from metric_runtime.stores.postgres import PostgresRuntimeStore

    store = PostgresRuntimeStore(pg_dsn, schema=pg_schema, min_size=1, max_size=2)
    try:
        with pytest.raises(RuntimeStoreNotMigratedError, match="store migrate"):
            store.ensure_migrated()
        store.migrate()
        store.ensure_migrated()
    finally:
        store.close()


def test_checksum_mismatch_is_a_hard_error(pg_dsn, pg_schema):
    original = Migration(1, "things", "CREATE TABLE things (id INT PRIMARY KEY);")
    edited = Migration(1, "things", "CREATE TABLE things (id BIGINT PRIMARY KEY);")
    with _connect(pg_dsn) as conn:
        migrate(conn, pg_schema, [original])
        with pytest.raises(MigrationChecksumError, match="001_things"):
            pending_migrations(conn, pg_schema, [edited])
        with pytest.raises(MigrationChecksumError):
            migrate(conn, pg_schema, [edited])


def test_failing_migration_rolls_back_whole_batch(pg_dsn, pg_schema):
    good = Migration(1, "good", "CREATE TABLE good_table (id INT PRIMARY KEY);")
    bad = Migration(2, "bad", "CREATE TABLE broken (id INT PRIMARY KEY; -- syntax error")
    with _connect(pg_dsn) as conn:
        with pytest.raises(Exception):  # noqa: B017 - psycopg SyntaxError
            migrate(conn, pg_schema, [good, bad])
        assert applied_migrations(conn, pg_schema) == {}
        row = conn.execute("SELECT to_regclass(%s)", (f"{pg_schema}.good_table",)).fetchone()
        assert row is not None and row[0] is None
        # After fixing the broken file, the whole batch applies cleanly.
        fixed = Migration(2, "bad", "CREATE TABLE broken (id INT PRIMARY KEY);")
        assert [m.version for m in migrate(conn, pg_schema, [good, fixed])] == [1, 2]


def test_concurrent_migrate_applies_each_file_once(pg_dsn, pg_schema):
    migrations = [
        Migration(1, "a", "CREATE TABLE a (id INT PRIMARY KEY); SELECT pg_sleep(0.3);"),
        Migration(2, "b", "CREATE TABLE b (id INT PRIMARY KEY);"),
    ]
    barrier = threading.Barrier(3)
    applied: list[list[int]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            with _connect(pg_dsn) as conn:
                barrier.wait(timeout=5)
                result = migrate(conn, pg_schema, migrations)
            with lock:
                applied.append([m.version for m in result])
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert sorted(applied) == [[], [], [1, 2]]
    with _connect(pg_dsn) as conn:
        assert set(applied_migrations(conn, pg_schema)) == {1, 2}
