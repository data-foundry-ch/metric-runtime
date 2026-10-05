"""Numbered, checksummed schema migrations — platform-neutral part.

Adapters with a versioned runtime schema ship ``NNN_name.sql`` files in a
package and record applied versions with checksums in a ledger. How the
ledger is stored and how migrations are applied atomically is up to the
adapter (see ``metric_runtime.adapters.postgres.migrations``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from importlib import resources

from metric_runtime.exceptions import ConfigurationError

__all__ = [
    "Migration",
    "MigrationChecksumError",
    "SchemaStatus",
    "load_migrations",
    "verify_migrations",
]

_FILE_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


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


@dataclass(frozen=True)
class SchemaStatus:
    """Applied / pending migrations of a managed runtime store."""

    namespace: str
    applied: dict[int, tuple[str, str]] = field(default_factory=dict)
    pending: list[Migration] = field(default_factory=list)

    @property
    def up_to_date(self) -> bool:
        return not self.pending


def load_migrations(package: str) -> list[Migration]:
    """Load ``NNN_name.sql`` files from ``package`` in version order (LF line endings)."""
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


def verify_migrations(
    applied: dict[int, tuple[str, str]], migrations: list[Migration]
) -> list[Migration]:
    """Return pending migrations; raise if the ledger disagrees with the code."""
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
