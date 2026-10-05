"""Back-compat location — see :mod:`metric_runtime.migrations` and the Postgres adapter."""

from metric_runtime.adapters.postgres.migrations import (
    POSTGRES_MIGRATIONS_PACKAGE,
    Migration,
    MigrationChecksumError,
    applied_migrations,
    load_migrations,
    migrate,
    pending_migrations,
    validate_schema_name,
)

__all__ = [
    "POSTGRES_MIGRATIONS_PACKAGE",
    "Migration",
    "MigrationChecksumError",
    "applied_migrations",
    "load_migrations",
    "migrate",
    "pending_migrations",
    "validate_schema_name",
]
