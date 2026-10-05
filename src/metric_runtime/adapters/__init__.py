"""Platform adapters.

Core Metric Runtime is platform-neutral. Each data platform (or notification
channel) plugs in through an adapter resolved from the connection ``type``.
Built-in adapters: ``postgres`` (reference full adapter), ``duckdb`` (local
analytical source), ``memory`` (process-local runtime store) and ``webhook``
(notifier). See ``docs/adapters.md`` for writing your own.
"""

from metric_runtime.adapters.base import (
    ROLES,
    AdapterCapabilities,
    AdapterContext,
    BaseAdapter,
    MetricRuntimeAdapter,
    Role,
)
from metric_runtime.adapters.registry import (
    ENTRY_POINT_GROUP,
    adapters_supporting,
    get_adapter,
    register_adapter,
    registered_adapters,
    unregister_adapter,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "ROLES",
    "AdapterCapabilities",
    "AdapterContext",
    "BaseAdapter",
    "MetricRuntimeAdapter",
    "Role",
    "adapters_supporting",
    "get_adapter",
    "register_adapter",
    "registered_adapters",
    "unregister_adapter",
]
