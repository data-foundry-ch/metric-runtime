"""In-process runtime store (``runtime_store`` only; not durable, single process)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from metric_runtime.adapters.base import AdapterCapabilities, AdapterContext, BaseAdapter

__all__ = ["MemoryAdapter", "MemoryStateStoreConfig"]


class MemoryStateStoreConfig(BaseModel):
    type: Literal["memory"] = "memory"


class MemoryAdapter(BaseAdapter):
    type_name = "memory"
    capabilities = AdapterCapabilities(runtime_store=True)
    config_model = MemoryStateStoreConfig

    def build_runtime_store(self, config: Any, context: AdapterContext) -> Any:
        from metric_runtime.stores.memory import InMemoryRuntimeStore

        return InMemoryRuntimeStore()
