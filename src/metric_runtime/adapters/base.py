"""Adapter contract: how a data platform plugs into Metric Runtime.

An adapter is resolved from a connection's ``type`` and turns a validated
connection config into role-specific runtime resources:

- ``metric_source`` → a :class:`~metric_runtime.execution.base.MetricExecutor`
- ``runtime_store`` → a :class:`~metric_runtime.stores.base.RuntimeStore`
- ``notifier``      → a :class:`~metric_runtime.notifications.base.Notifier`

An adapter supports any subset of roles. When one connection is used for
several roles, each ``build_*`` call returns its own resources (connections,
pools, sessions): a read-only source session is never shared with the
writable runtime store.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from metric_runtime.exceptions import UnsupportedRoleError

if TYPE_CHECKING:
    from metric_runtime.config.models import MetricRuntimeProjectConfig
    from metric_runtime.execution.base import MetricExecutor
    from metric_runtime.notifications.base import Notifier
    from metric_runtime.stores.base import RuntimeStore

__all__ = [
    "ROLES",
    "AdapterCapabilities",
    "AdapterContext",
    "BaseAdapter",
    "MetricRuntimeAdapter",
    "Role",
]

Role = Literal["metric_source", "runtime_store", "notifier"]
ROLES: tuple[Role, ...] = ("metric_source", "runtime_store", "notifier")


@dataclass(frozen=True)
class AdapterCapabilities:
    """What an adapter can do; used for profile validation and messages."""

    metric_source: bool = False
    runtime_store: bool = False
    notifier: bool = False
    # Runtime conclusions survive process restarts.
    durable: bool = False
    # Several workers may safely share one runtime store.
    distributed_claims: bool = False
    # The runtime store has a versioned schema (``store migrate`` / ``store status``).
    migrations: bool = False

    def supports(self, role: str) -> bool:
        return role in ROLES and bool(getattr(self, role))

    def names(self) -> list[str]:
        return [f.name for f in fields(self) if getattr(self, f.name)]


@dataclass(frozen=True)
class AdapterContext:
    """Project-level information an adapter may need while building resources."""

    project: MetricRuntimeProjectConfig
    base_dir: Path
    connection_name: str | None = None


@runtime_checkable
class MetricRuntimeAdapter(Protocol):
    """Platform adapter registered under a connection ``type``."""

    type_name: str
    capabilities: AdapterCapabilities
    config_model: type[BaseModel]
    install_hint: str | None

    def build_executor(self, config: Any, context: AdapterContext) -> MetricExecutor: ...

    def build_runtime_store(self, config: Any, context: AdapterContext) -> RuntimeStore: ...

    def build_notifier(self, config: Any, context: AdapterContext) -> Notifier: ...

    def role_conflicts(
        self,
        *,
        source_type: str,
        source_config: BaseModel,
        runtime_config: Any,
        same_connection: bool,
    ) -> list[str]:
        """Reasons why runtime-owned objects would collide with the source.

        Called on the ``runtime_store`` adapter when a profile has both roles.
        The adapter defines the boundary for its platform (separate schema,
        dataset, table prefix, ...). Return ``[]`` when they cannot collide.
        """
        ...

    def unsupported_role_hint(self, role: str) -> str | None: ...


class BaseAdapter:
    """Convenience base: unsupported roles raise :class:`UnsupportedRoleError`."""

    type_name: str = ""
    capabilities: AdapterCapabilities = AdapterCapabilities()
    config_model: type[BaseModel]
    install_hint: str | None = None
    # Extra explanation when a profile wires this adapter into an unsupported role.
    role_hints: Mapping[str, str] = {}

    def _unsupported(self, role: str) -> UnsupportedRoleError:
        hint = self.unsupported_role_hint(role)
        message = f"Adapter {self.type_name!r} does not support the {role} role"
        return UnsupportedRoleError(f"{message}: {hint}" if hint else message)

    def build_executor(self, config: Any, context: AdapterContext) -> MetricExecutor:
        raise self._unsupported("metric_source")

    def build_runtime_store(self, config: Any, context: AdapterContext) -> RuntimeStore:
        raise self._unsupported("runtime_store")

    def build_notifier(self, config: Any, context: AdapterContext) -> Notifier:
        raise self._unsupported("notifier")

    def role_conflicts(
        self,
        *,
        source_type: str,
        source_config: BaseModel,
        runtime_config: Any,
        same_connection: bool,
    ) -> list[str]:
        return []

    def unsupported_role_hint(self, role: str) -> str | None:
        return self.role_hints.get(role)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} type={self.type_name!r}>"
