"""``type: postgres`` connection config (``metric_source`` and/or ``runtime_store``)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from metric_runtime.config.duration import parse_duration

__all__ = ["PostgresConnectionConfig"]


def _positive_duration(value: str, *, field: str) -> str:
    if parse_duration(value, field=field).total_seconds() <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return value


def _parse_conninfo(dsn: str) -> dict[str, Any] | None:
    """Parse a libpq DSN (URL or ``key=value``) without connecting."""
    if not dsn:
        return {}
    try:
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        pass
    else:
        try:
            return dict(conninfo_to_dict(dsn))
        except Exception:  # noqa: BLE001
            return None
    from urllib.parse import unquote, urlsplit

    if "://" not in dsn:
        try:
            return dict(part.split("=", 1) for part in dsn.split())
        except ValueError:
            return None
    parts = urlsplit(dsn)
    return {
        "host": parts.hostname,
        "port": parts.port,
        "dbname": unquote(parts.path.lstrip("/")) or None,
        "user": parts.username,
    }


def _identifier(value: str, *, field: str) -> str:
    from metric_runtime.adapters.postgres.migrations import validate_schema_name
    from metric_runtime.exceptions import ConfigurationError

    try:
        return validate_schema_name(value)
    except ConfigurationError as exc:
        raise ValueError(f"{field}: {exc}") from exc


class PostgresConnectionConfig(BaseModel):
    """One Postgres connection, usable as ``metric_source`` and/or ``runtime_store``.

    Give either ``dsn`` or ``host`` + ``database``. Discrete fields override
    the same keys in ``dsn``.

    - ``source_schema``: where business data is read from. Unqualified
      ``fact_table`` names and SQL calculations resolve in this schema.
    - ``runtime_schema``: where Metric Runtime writes its tables (default
      ``metric_runtime``; ``schema`` is the deprecated name).

    The same connection may serve both roles; the effective source schema
    must then differ from ``runtime_schema``.
    """

    type: Literal["postgres"] = "postgres"
    dsn: SecretStr | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    password: SecretStr | None = None
    sslmode: str | None = None
    connect_timeout: int | None = Field(default=10, ge=1)
    # Runtime-store role.
    runtime_schema: str = Field(
        default="metric_runtime",
        validation_alias=AliasChoices("runtime_schema", "schema"),
    )
    claim_ttl: str = "15m"
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=10, ge=1)
    # Source role.
    source_schema: str | None = None
    fact_table: str | None = None
    timestamp_column: str = "ts"
    statement_timeout: str = "30s"

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _schema_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "schema" in data and "runtime_schema" in data:
            if data["schema"] != data["runtime_schema"]:
                raise ValueError(
                    "postgres connection sets both runtime_schema and schema "
                    "(deprecated alias) to different values; use runtime_schema only"
                )
        return data

    @field_validator("claim_ttl")
    @classmethod
    def _validate_claim_ttl(cls, value: str) -> str:
        return _positive_duration(value, field="postgres.claim_ttl")

    @field_validator("statement_timeout")
    @classmethod
    def _validate_statement_timeout(cls, value: str) -> str:
        return _positive_duration(value, field="postgres.statement_timeout")

    @field_validator("runtime_schema")
    @classmethod
    def _validate_runtime_schema(cls, value: str) -> str:
        return _identifier(value, field="runtime_schema")

    @field_validator("source_schema")
    @classmethod
    def _validate_source_schema(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, field="source_schema")

    @model_validator(mode="after")
    def _require_target(self) -> PostgresConnectionConfig:
        if self.dsn is None and not (self.host and self.database):
            raise ValueError("postgres connection requires 'dsn' or 'host' + 'database'")
        return self

    @property
    def schema_name(self) -> str:
        """Deprecated name of :attr:`runtime_schema`."""
        return self.runtime_schema

    @property
    def effective_fact_table(self) -> str | None:
        """``fact_table`` qualified with ``source_schema`` when it is unqualified."""
        if self.fact_table and "." not in self.fact_table and self.source_schema:
            return f"{self.source_schema}.{self.fact_table}"
        return self.fact_table

    @property
    def effective_source_schema(self) -> str:
        """Schema business data is actually read from.

        A schema-qualified ``fact_table`` wins over ``source_schema``;
        otherwise ``source_schema``, defaulting to ``public``.
        """
        if self.fact_table and "." in self.fact_table:
            return self.fact_table.rsplit(".", 1)[0].strip('"')
        return self.source_schema or "public"

    def database_identity(self) -> tuple[str, int, str] | None:
        """``(host, port, dbname)`` used to detect two roles on one database.

        ``None`` when it cannot be determined offline (e.g. unparseable dsn).
        """
        params: dict[str, Any] = {}
        if self.dsn is not None:
            parsed = _parse_conninfo(self.dsn.get_secret_value())
            if parsed is None:
                return None
            params = parsed
        params.update(self.connect_kwargs())
        host = str(params.get("host") or "localhost").lower()
        try:
            port = int(params.get("port") or 5432)
        except (TypeError, ValueError):
            return None
        dbname = params.get("dbname") or params.get("user")
        if not dbname:
            return None
        return host, port, str(dbname)

    def conninfo(self) -> str:
        return self.dsn.get_secret_value() if self.dsn is not None else ""

    def connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "password": self.password.get_secret_value() if self.password else None,
            "sslmode": self.sslmode,
            "connect_timeout": self.connect_timeout,
        }
        return {k: v for k, v in kwargs.items() if v is not None}
