"""State persistence adapters."""

from metric_runtime.stores.base import StateStore
from metric_runtime.stores.memory import InMemoryStateStore

__all__ = ["StateStore", "InMemoryStateStore"]
