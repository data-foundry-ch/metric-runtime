"""Postgres runtime-store migrations (``NNN_name.sql`` files in this package).

All pending migrations run in ONE transaction under a transaction-scoped
advisory lock (Postgres DDL is transactional): either every pending file is
applied and recorded, or nothing is. Concurrent ``migrate`` calls serialize
on the lock; the second one finds nothing pending.

Migration files must therefore be transaction-safe (no ``CREATE INDEX
CONCURRENTLY``, no ``VACUUM``).
"""

from __future__ import annotations

import re
from typing import Any

from metric_runtime.exceptions import ConfigurationError
from metric_runtime.migrations import (
    Migration,
    MigrationChecksumError,
    verify_migrations,
)
from metric_runtime.migrations import load_migrations as _load_package_migrations

__all__ = [
    "Migration",
    "MigrationChecksumError",
    "POSTGRES_MIGRATIONS_PACKAGE",
    "applied_migrations",
    "load_migrations",
    "migrate",
    "pending_migrations",
    "validate_schema_name",
]

POSTGRES_MIGRATIONS_PACKAGE = __name__
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def validate_schema_name(schema: str) -> str:
    if not isinstance(schema, str) or not _SCHEMA_RE.fullmatch(schema):
        raise ConfigurationError(
            f"Invalid runtime store schema name {schema!r}: expected a plain identifier"
        )
    return schema


def load_migrations(package: str = POSTGRES_MIGRATIONS_PACKAGE) -> list[Migration]:
    """Load the packaged Postgres migrations in version order."""
    return _load_package_migrations(package)


def _sql():
    from psycopg import sql

    return sql


def _schema_exists_with_ledger(conn: Any, schema: str) -> bool:
    row = conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL",
        (f"{schema}.schema_migrations",),
    ).fetchone()
    return bool(row and row[0])


def applied_migrations(conn: Any, schema: str) -> dict[int, tuple[str, str]]:
    """Return ``{version: (name, checksum)}`` for applied migrations."""
    schema = validate_schema_name(schema)
    if not _schema_exists_with_ledger(conn, schema):
        return {}
    sql = _sql()
    rows = conn.execute(
        sql.SQL("SELECT version, name, checksum FROM {}.schema_migrations").format(
            sql.Identifier(schema)
        )
    ).fetchall()
    return {int(r[0]): (str(r[1]), str(r[2])) for r in rows}


def pending_migrations(
    conn: Any,
    schema: str,
    migrations: list[Migration] | None = None,
) -> list[Migration]:
    """Pending migrations (raises on checksum mismatch). Read-only."""
    migrations = load_migrations() if migrations is None else migrations
    return verify_migrations(applied_migrations(conn, schema), migrations)


def migrate(
    conn: Any,
    schema: str,
    migrations: list[Migration] | None = None,
) -> list[Migration]:
    """Apply all pending migrations in one transaction. Returns what was applied."""
    schema = validate_schema_name(schema)
    migrations = load_migrations() if migrations is None else migrations
    sql = _sql()
    ident = sql.Identifier(schema)
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"metric_runtime.migrations:{schema}",),
        )
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(ident))
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(ident))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        pending = verify_migrations(applied_migrations(conn, schema), migrations)
        for migration in pending:
            conn.execute(migration.sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
    return pending
