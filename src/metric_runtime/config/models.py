"""Configuration models.

Semantics live with the project.
Connections live with the environment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from metric_runtime.config.duration import parse_duration


class ProjectMeta(BaseModel):
    name: str = "metric-runtime"
    description: str | None = None


class CatalogConfig(BaseModel):
    path: str | None = None
    module: str | None = None


class RuntimeConfig(BaseModel):
    evaluation_interval: str = "15m"
    default_profile: str = "local"
    fact_table: str | None = None

    @field_validator("evaluation_interval")
    @classmethod
    def _validate_interval(cls, value: str) -> str:
        parse_duration(value, field="runtime.evaluation_interval")
        return value


class InvestigationConfig(BaseModel):
    max_depth: int = 5
    min_support: int = 100


class StatePolicyConfig(BaseModel):
    detections_before_open: int = 2
    resolve_after_healthy_windows: int = 2
    cooldown: str = "30m"
    min_impact_eur: float = 50.0

    @field_validator("cooldown")
    @classmethod
    def _validate_cooldown(cls, value: str) -> str:
        parse_duration(value, field="state.cooldown")
        return value


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


# Documented for future adapters — not implemented in v0.1.
class UnsupportedConnectionConfig(BaseModel):
    type: str
    model_config = {"extra": "allow"}

    @field_validator("type")
    @classmethod
    def _reject_known_unsupported(cls, value: str) -> str:
        unsupported = {"snowflake", "postgres", "bigquery", "databricks"}
        if value in unsupported:
            raise ValueError(
                f"Connection type {value!r} is documented as a future adapter "
                "and is not implemented in metric-runtime v0.1"
            )
        raise ValueError(f"Unknown connection type: {value!r}")


ConnectionConfig = Annotated[
    DuckDBConnectionConfig | MemoryStateStoreConfig | UnsupportedConnectionConfig,
    Field(discriminator="type"),
]


class InlineMemoryStateStore(BaseModel):
    type: Literal["memory"] = "memory"


class ProfileConfig(BaseModel):
    metric_source: str | None = None
    state_store: str | InlineMemoryStateStore | None = None


class ConnectionsFile(BaseModel):
    connections: dict[str, dict[str, Any]] = Field(default_factory=dict)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)

    def get_connection(self, name: str) -> DuckDBConnectionConfig | MemoryStateStoreConfig:
        if name not in self.connections:
            from metric_runtime.exceptions import UnknownConnectionError

            raise UnknownConnectionError(f"Unknown connection: {name!r}")
        raw = dict(self.connections[name])
        ctype = raw.get("type")
        if ctype == "duckdb":
            return DuckDBConnectionConfig.model_validate(raw)
        if ctype == "memory":
            return MemoryStateStoreConfig.model_validate(raw)
        from metric_runtime.exceptions import UnsupportedConnectionTypeError

        unsupported = {"snowflake", "postgres", "bigquery", "databricks"}
        if ctype in unsupported:
            raise UnsupportedConnectionTypeError(
                f"Connection type {ctype!r} is documented as a future adapter "
                "and is not implemented in metric-runtime v0.1"
            )
        raise UnsupportedConnectionTypeError(f"Unknown connection type: {ctype!r}")


class SecretFieldDemo(BaseModel):
    """Used in tests to prove SecretStr never leaks via repr."""

    password: SecretStr
