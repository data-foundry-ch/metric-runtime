"""Numbered SQL migrations for durable runtime stores.

All pending migrations run in ONE transaction under a transaction-scoped
advisory lock (Postgres DDL is transactional): either every pending file is
applied and recorded, or nothing is. Concurrent ``migrate`` calls serialize
on the lock; the second one finds nothing pending.

Migration files must therefore be transaction-safe (no ``CREATE INDEX
CONCURRENTLY``, no ``VACUUM``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any

from metric_runtime.exceptions import ConfigurationError

__all__ = [
    "Migration",
    "MigrationChecksumError",
    "applied_migrations",
    "load_migrations",
    "migrate",
    "pending_migrations",
    "validate_schema_name",
]

POSTGRES_MIGRATIONS_PACKAGE = "metric_runtime.stores.migrations.postgres"
_FILE_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class MigrationChecksumError(ConfigurationError):
    """An already-applied migration file was modified."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def filename(self) -> str:
        return f"{self.version:03d}_{self.name}.sql"


def validate_schema_name(schema: str) -> str:
    if not isinstance(schema, str) or not _SCHEMA_RE.fullmatch(schema):
        raise ConfigurationError(
            f"Invalid runtime store schema name {schema!r}: expected a plain identifier"
        )
    return schema


def load_migrations(package: str = POSTGRES_MIGRATIONS_PACKAGE) -> list[Migration]:
    """Load ``NNN_name.sql`` files in version order (line endings normalized)."""
    migrations: list[Migration] = []
    for entry in resources.files(package).iterdir():
        match = _FILE_RE.match(entry.name)
        if match is None:
            continue
        text = entry.read_text(encoding="utf-8").replace("\r\n", "\n")
        migrations.append(Migration(version=int(match.group(1)), name=match.group(2), sql=text))
    migrations.sort(key=lambda m: m.version)
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise ConfigurationError(f"Duplicate migration versions in {package}: {versions}")
    return migrations


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


def _verify(applied: dict[int, tuple[str, str]], migrations: list[Migration]) -> list[Migration]:
    known = {m.version: m for m in migrations}
    for version, (name, checksum) in applied.items():
        migration = known.get(version)
        if migration is None:
            raise ConfigurationError(
                f"Runtime store has migration {version:03d}_{name} which this version of "
                "metric-runtime does not know (database is newer than the code)"
            )
        if migration.checksum != checksum:
            raise MigrationChecksumError(
                f"Migration {migration.filename} was modified after it was applied "
                f"(checksum {checksum[:12]} in database, {migration.checksum[:12]} in code)"
            )
    return [m for m in migrations if m.version not in applied]


def pending_migrations(
    conn: Any,
    schema: str,
    migrations: list[Migration] | None = None,
) -> list[Migration]:
    """Pending migrations (raises on checksum mismatch). Read-only."""
    migrations = load_migrations() if migrations is None else migrations
    return _verify(applied_migrations(conn, schema), migrations)


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
        pending = _verify(applied_migrations(conn, schema), migrations)
        for migration in pending:
            conn.execute(migration.sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
    return pending
