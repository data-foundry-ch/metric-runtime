"""Configuration models.

Semantics live with the project.
Connections live with the environment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, SecretStr, field_validator, model_validator

from metric_runtime.config.duration import parse_duration


class ProjectMeta(BaseModel):
    name: str = "metric-runtime"
    description: str | None = None


class CatalogConfig(BaseModel):
    """Semantic catalog loading.

    Prefer ``entrypoint: python.module.path:attribute`` pointing at a
    ``MetricCatalog`` (or ``build_catalog`` / ``CATALOG`` on a module).
    ``module`` remains as a deprecated alias for entrypoint without an
    attribute suffix.
    """

    entrypoint: str | None = None
    path: str | None = None
    module: str | None = None

    @model_validator(mode="after")
    def _normalize_entrypoint(self) -> CatalogConfig:
        if self.entrypoint is None and self.module:
            object.__setattr__(self, "entrypoint", self.module)
        return self


def _positive_duration(value: str, *, field: str) -> str:
    if parse_duration(value, field=field).total_seconds() <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return value


class ScheduleConfig(BaseModel):
    """Per-metric schedule override (global scope)."""

    evaluation_interval: str | None = None
    enabled: bool = True

    @field_validator("evaluation_interval")
    @classmethod
    def _validate_interval(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _positive_duration(value, field="schedules.evaluation_interval")


class NotificationRuntimeConfig(BaseModel):
    """Outbox delivery policy for scheduled runs."""

    max_attempts: int | None = Field(default=10, ge=1)
    backoff_initial: str = "30s"
    backoff_max: str = "1h"
    lease: str = "5m"

    @field_validator("backoff_initial", "backoff_max")
    @classmethod
    def _validate_backoff(cls, value: str) -> str:
        parse_duration(value, field="runtime.notifications backoff")
        return value

    @field_validator("lease")
    @classmethod
    def _validate_lease(cls, value: str) -> str:
        return _positive_duration(value, field="runtime.notifications.lease")


class RuntimeConfig(BaseModel):
    evaluation_interval: str = "15m"
    default_profile: str = "local"
    fact_table: str | None = None
    # Wait this long after a window closes before evaluating it (late data).
    evaluation_lag: str = "0m"
    # How many missed windows per metric are (re)tried; older ones are skipped.
    max_catchup_windows: int = Field(default=1, ge=1)
    # Upper bound on any sleep in continuous mode.
    idle_interval: str = "5m"
    schedules: dict[str, ScheduleConfig] = Field(default_factory=dict)
    notifications: NotificationRuntimeConfig = Field(default_factory=NotificationRuntimeConfig)

    @field_validator("evaluation_interval")
    @classmethod
    def _validate_interval(cls, value: str) -> str:
        return _positive_duration(value, field="runtime.evaluation_interval")

    @field_validator("idle_interval")
    @classmethod
    def _validate_idle(cls, value: str) -> str:
        return _positive_duration(value, field="runtime.idle_interval")

    @field_validator("evaluation_lag")
    @classmethod
    def _validate_lag(cls, value: str) -> str:
        parse_duration(value, field="runtime.evaluation_lag")
        return value


class InvestigationConfig(BaseModel):
    max_depth: int = 5
    min_support: int = 100


class StatePolicyConfig(BaseModel):
    detections_before_open: int = 2
    resolve_after_healthy_windows: int = 2
    cooldown: str = "30m"
    min_impact: float = 50.0
    # Deprecated alias accepted on load.
    min_impact_eur: float | None = None

    @field_validator("cooldown")
    @classmethod
    def _validate_cooldown(cls, value: str) -> str:
        parse_duration(value, field="state.cooldown")
        return value

    @model_validator(mode="after")
    def _normalize_min_impact(self) -> StatePolicyConfig:
        if self.min_impact_eur is not None:
            object.__setattr__(self, "min_impact", float(self.min_impact_eur))
        return self


class MetricRuntimeProjectConfig(BaseModel):
    project: ProjectMeta = Field(default_factory=ProjectMeta)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    investigation: InvestigationConfig = Field(default_factory=InvestigationConfig)
    state: StatePolicyConfig = Field(default_factory=StatePolicyConfig)


class DuckDBConnectionConfig(BaseModel):
    type: Literal["duckdb"] = "duckdb"
    path: Path | str | None = None
    fact_table: str | None = None
    read_only: bool = True


class MemoryStateStoreConfig(BaseModel):
    type: Literal["memory"] = "memory"


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


class PostgresConnectionConfig(BaseModel):
    """Postgres connection — ``runtime_store`` and/or read-only ``metric_source`` role.

    Give either ``dsn`` or ``host`` + ``database``. Discrete fields override
    the same keys in ``dsn``.

    Runtime-store role: tables live in ``schema`` (default ``metric_runtime``).
    Source role: ``fact_table`` (optionally ``schema.table``) for formula
    metrics, ``timestamp_column``, ``statement_timeout``. When both roles
    resolve to the same database, the runtime schema must differ from the
    source schema.
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
    schema_name: str = Field(default="metric_runtime", alias="schema")
    claim_ttl: str = "15m"
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=10, ge=1)
    # Source role.
    fact_table: str | None = None
    timestamp_column: str = "ts"
    statement_timeout: str = "30s"

    model_config = {"populate_by_name": True}

    @field_validator("claim_ttl")
    @classmethod
    def _validate_claim_ttl(cls, value: str) -> str:
        return _positive_duration(value, field="postgres.claim_ttl")

    @field_validator("statement_timeout")
    @classmethod
    def _validate_statement_timeout(cls, value: str) -> str:
        return _positive_duration(value, field="postgres.statement_timeout")

    @property
    def source_schema(self) -> str:
        """Schema of ``fact_table`` (``public`` when unqualified or unset)."""
        if self.fact_table and "." in self.fact_table:
            return self.fact_table.rsplit(".", 1)[0]
        return "public"

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

    @field_validator("schema_name")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        from metric_runtime.exceptions import ConfigurationError
        from metric_runtime.stores.migrations import validate_schema_name

        try:
            return validate_schema_name(value)
        except ConfigurationError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _require_target(self) -> PostgresConnectionConfig:
        if self.dsn is None and not (self.host and self.database):
            raise ValueError("postgres connection requires 'dsn' or 'host' + 'database'")
        return self

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


class WebhookConnectionConfig(BaseModel):
    """HTTP(S) JSON webhook for outbox delivery (``notifier`` role)."""

    type: Literal["webhook"] = "webhook"
    url: SecretStr
    secret: SecretStr | None = None
    headers: dict[str, SecretStr] = Field(default_factory=dict)
    timeout: float = Field(default=10.0, gt=0)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: SecretStr) -> SecretStr:
        from urllib.parse import urlsplit

        parts = urlsplit(value.get_secret_value())
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("webhook url must be an absolute http(s) URL")
        return value


_FUTURE_CONNECTION_TYPES = {"snowflake", "bigquery", "databricks"}


# Documented for future adapters — not implemented yet.
class UnsupportedConnectionConfig(BaseModel):
    type: str
    model_config = {"extra": "allow"}

    @field_validator("type")
    @classmethod
    def _reject_known_unsupported(cls, value: str) -> str:
        if value in _FUTURE_CONNECTION_TYPES:
            raise ValueError(
                f"Connection type {value!r} is documented as a future adapter "
                "and is not implemented in this metric-runtime release"
            )
        raise ValueError(f"Unknown connection type: {value!r}")


ConnectionConfig = Annotated[
    DuckDBConnectionConfig
    | MemoryStateStoreConfig
    | PostgresConnectionConfig
    | WebhookConnectionConfig
    | UnsupportedConnectionConfig,
    Field(discriminator="type"),
]

AnyConnectionConfig = (
    DuckDBConnectionConfig
    | MemoryStateStoreConfig
    | PostgresConnectionConfig
    | WebhookConnectionConfig
)


class InlineMemoryStateStore(BaseModel):
    type: Literal["memory"] = "memory"


class InlineNotifier(BaseModel):
    """Built-in notifiers that need no connection: ``logging`` (default) or ``none``."""

    type: Literal["logging", "none"] = "logging"

    @field_validator("type", mode="before")
    @classmethod
    def _yaml_null_means_none(cls, value: Any) -> Any:
        # ``type: null`` in YAML parses to None.
        return "none" if value is None or value == "null" else value


class ProfileConfig(BaseModel):
    """Role wiring for one environment.

    ``runtime_store`` holds runtime conclusions (state, evaluations, incidents,
    outbox). ``state_store`` is the deprecated name for the same role.
    ``notifier`` delivers outbox events: a ``type: webhook`` connection name or
    an inline ``{type: logging|none}`` (default: logging).
    """

    metric_source: str | None = None
    runtime_store: str | InlineMemoryStateStore | None = None
    state_store: str | InlineMemoryStateStore | None = None
    notifier: str | InlineNotifier | None = None

    @model_validator(mode="after")
    def _normalize_runtime_store(self) -> ProfileConfig:
        legacy = self.state_store
        if legacy is None:
            return self
        if self.runtime_store is not None and self.runtime_store != legacy:
            raise ValueError(
                "Profile sets both runtime_store and state_store (deprecated alias) "
                "to different values; use runtime_store only"
            )
        object.__setattr__(self, "runtime_store", legacy)
        return self


class ConnectionsFile(BaseModel):
    connections: dict[str, dict[str, Any]] = Field(default_factory=dict)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    # Connections whose ${ENV} placeholders could not be resolved at load time.
    _unresolved: dict[str, Exception] = PrivateAttr(default_factory=dict)

    def get_connection(self, name: str) -> AnyConnectionConfig:
        from metric_runtime.exceptions import (
            ConfigurationError,
            UnknownConnectionError,
            UnsupportedConnectionTypeError,
        )

        if name not in self.connections:
            raise UnknownConnectionError(f"Unknown connection: {name!r}")
        if name in self._unresolved:
            raise self._unresolved[name]
        raw = dict(self.connections[name])
        ctype = raw.get("type")
        model: type[BaseModel]
        if ctype == "duckdb":
            model = DuckDBConnectionConfig
        elif ctype == "memory":
            model = MemoryStateStoreConfig
        elif ctype == "postgres":
            model = PostgresConnectionConfig
        elif ctype == "webhook":
            model = WebhookConnectionConfig
        elif ctype in _FUTURE_CONNECTION_TYPES:
            raise UnsupportedConnectionTypeError(
                f"Connection type {ctype!r} is documented as a future adapter "
                "and is not implemented in this metric-runtime release"
            )
        else:
            raise UnsupportedConnectionTypeError(f"Unknown connection type: {ctype!r}")
        try:
            return model.model_validate(raw)
        except ValueError as exc:
            raise ConfigurationError(f"Invalid connection {name!r}: {exc}") from exc


class SecretFieldDemo(BaseModel):
    """Used in tests to prove SecretStr never leaks via repr."""

    password: SecretStr
