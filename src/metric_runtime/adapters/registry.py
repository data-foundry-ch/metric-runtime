"""Adapter registry: connection ``type`` → :class:`MetricRuntimeAdapter`.

Built-in adapters are registered as import paths and only imported on first
use, so ``import metric_runtime`` never imports a database driver.

External packages register adapters without touching core code, either

- explicitly: ``register_adapter("snowflake", SnowflakeAdapter)``, or
- via the ``metric_runtime.adapters`` entry-point group, e.g. in the adapter
  package's ``pyproject.toml``::

      [project.entry-points."metric_runtime.adapters"]
      snowflake = "metric_runtime_snowflake:SnowflakeAdapter"

Entry points are discovered the first time an unregistered type is requested.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from typing import Any

from metric_runtime.adapters.base import MetricRuntimeAdapter
from metric_runtime.exceptions import ConfigurationError, UnsupportedConnectionTypeError

__all__ = [
    "ENTRY_POINT_GROUP",
    "adapters_supporting",
    "get_adapter",
    "register_adapter",
    "registered_adapters",
    "unregister_adapter",
]

ENTRY_POINT_GROUP = "metric_runtime.adapters"

AdapterSpec = MetricRuntimeAdapter | type | Callable[[], MetricRuntimeAdapter] | str

_BUILTIN_ADAPTERS: dict[str, str] = {
    "duckdb": "metric_runtime.adapters.duckdb.adapter:DuckDBAdapter",
    "memory": "metric_runtime.adapters.memory.adapter:MemoryAdapter",
    "postgres": "metric_runtime.adapters.postgres.adapter:PostgresAdapter",
    "webhook": "metric_runtime.adapters.webhook.adapter:WebhookAdapter",
}

# Platforms with planned external adapter packages (better error messages).
_KNOWN_EXTERNAL = {"snowflake", "bigquery", "databricks"}

_lock = threading.RLock()
_specs: dict[str, Any] = dict(_BUILTIN_ADAPTERS)
_instances: dict[str, MetricRuntimeAdapter] = {}
_entry_points_loaded = False


def register_adapter(type_name: str, adapter: AdapterSpec, *, replace: bool = False) -> None:
    """Register ``adapter`` for connections with ``type: <type_name>``.

    ``adapter`` may be an adapter instance, an adapter class / zero-argument
    factory, or a ``"module:attribute"`` import path (imported lazily).
    """
    if not isinstance(type_name, str) or not type_name.strip():
        raise ConfigurationError("Adapter type name must be a non-empty string")
    with _lock:
        if type_name in _specs and not replace:
            raise ConfigurationError(
                f"An adapter is already registered for type {type_name!r}; "
                "pass replace=True to override it"
            )
        _specs[type_name] = adapter
        _instances.pop(type_name, None)


def unregister_adapter(type_name: str) -> None:
    """Remove a registration (built-ins are restored on next lookup)."""
    with _lock:
        _instances.pop(type_name, None)
        if type_name in _BUILTIN_ADAPTERS:
            _specs[type_name] = _BUILTIN_ADAPTERS[type_name]
        else:
            _specs.pop(type_name, None)


def _load_entry_points() -> None:
    global _entry_points_loaded
    if _entry_points_loaded:
        return
    _entry_points_loaded = True
    from importlib.metadata import entry_points

    for ep in entry_points(group=ENTRY_POINT_GROUP):
        _specs.setdefault(ep.name, ep)


def _materialize(type_name: str, spec: Any) -> MetricRuntimeAdapter:
    try:
        if isinstance(spec, str):
            module_name, _, attr = spec.partition(":")
            spec = getattr(importlib.import_module(module_name), attr)
        elif hasattr(spec, "load") and hasattr(spec, "group"):  # importlib.metadata.EntryPoint
            spec = spec.load()
        if isinstance(spec, type) or (callable(spec) and not hasattr(spec, "capabilities")):
            spec = spec()
    except ConfigurationError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface broken adapter packages clearly
        raise ConfigurationError(f"Adapter for type {type_name!r} failed to load: {exc}") from exc
    if not isinstance(spec, MetricRuntimeAdapter):
        raise ConfigurationError(
            f"Adapter registered for type {type_name!r} does not implement MetricRuntimeAdapter"
        )
    return spec


def _unknown_type_message(type_name: Any) -> str:
    if not type_name:
        return "Connection is missing 'type'"
    known = ", ".join(sorted(_specs))
    message = f"Unknown connection type: {type_name!r} (registered adapters: {known})"
    if type_name in _KNOWN_EXTERNAL:
        message += (
            f". No {type_name} adapter is installed; install one "
            f"(e.g. pip install metric-runtime-{type_name}) or register it with register_adapter()"
        )
    return message


def get_adapter(type_name: Any) -> MetricRuntimeAdapter:
    """Return the adapter for a connection ``type`` (raises if none is registered)."""
    with _lock:
        if isinstance(type_name, str) and type_name in _instances:
            return _instances[type_name]
        if not isinstance(type_name, str) or not type_name:
            raise UnsupportedConnectionTypeError(_unknown_type_message(type_name))
        if type_name not in _specs:
            _load_entry_points()
        if type_name not in _specs:
            raise UnsupportedConnectionTypeError(_unknown_type_message(type_name))
        adapter = _materialize(type_name, _specs[type_name])
        _instances[type_name] = adapter
        return adapter


def registered_adapters() -> list[str]:
    """All registered connection types (built-in, explicit and entry points)."""
    with _lock:
        _load_entry_points()
        return sorted(_specs)


def adapters_supporting(role: str) -> list[str]:
    """Registered types whose adapter supports ``role`` (broken adapters are skipped)."""
    names: list[str] = []
    for name in registered_adapters():
        try:
            if get_adapter(name).capabilities.supports(role):
                names.append(name)
        except ConfigurationError:
            continue
    return names
