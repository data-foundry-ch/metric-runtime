"""Configuration models.

Semantics live with the project.
Connections live with the environment.
"""

from __future__ import annotations

from typing import Any, Literal

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


# Connection models are adapter-defined (``adapter.config_model``); these
# names stay importable from here for backwards compatibility.
_ADAPTER_CONFIG_MODELS = {
    "DuckDBConnectionConfig": "metric_runtime.adapters.duckdb.config",
    "MemoryStateStoreConfig": "metric_runtime.adapters.memory.adapter",
    "PostgresConnectionConfig": "metric_runtime.adapters.postgres.config",
    "WebhookConnectionConfig": "metric_runtime.adapters.webhook.config",
}

AnyConnectionConfig = BaseModel


def __getattr__(name: str) -> Any:
    if name in _ADAPTER_CONFIG_MODELS:
        import importlib

        return getattr(importlib.import_module(_ADAPTER_CONFIG_MODELS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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

    def connection_type(self, name: str) -> Any:
        """Declared ``type`` of a connection (no validation, no env resolution)."""
        from metric_runtime.exceptions import UnknownConnectionError

        if name not in self.connections:
            raise UnknownConnectionError(f"Unknown connection: {name!r}")
        return self.connections[name].get("type")

    def get_connection(self, name: str) -> AnyConnectionConfig:
        """Validate a connection with its adapter's ``config_model``."""
        from metric_runtime.adapters.registry import get_adapter
        from metric_runtime.exceptions import ConfigurationError

        adapter = get_adapter(self.connection_type(name))
        if name in self._unresolved:
            raise self._unresolved[name]
        try:
            return adapter.config_model.model_validate(dict(self.connections[name]))
        except ValueError as exc:
            raise ConfigurationError(f"Invalid connection {name!r}: {exc}") from exc


class SecretFieldDemo(BaseModel):
    """Used in tests to prove SecretStr never leaks via repr."""

    password: SecretStr
