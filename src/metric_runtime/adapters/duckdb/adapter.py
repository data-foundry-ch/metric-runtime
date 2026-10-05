"""DuckDB — lightweight local analytical source (``metric_source`` only).

DuckDB is an embedded, single-writer database: it is not offered as a
runtime store, because shared claims, ordered commits and outbox leases
across workers are not safe on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from metric_runtime.adapters.base import AdapterCapabilities, AdapterContext, BaseAdapter
from metric_runtime.adapters.duckdb.config import DuckDBConnectionConfig
from metric_runtime.exceptions import ConfigurationError

__all__ = ["DuckDBAdapter"]


class DuckDBAdapter(BaseAdapter):
    type_name = "duckdb"
    capabilities = AdapterCapabilities(metric_source=True)
    config_model = DuckDBConnectionConfig
    install_hint = 'pip install "metric-runtime[duckdb]"'
    role_hints = {
        "runtime_store": (
            "DuckDB is an analytical source only; use a durable runtime store "
            "(e.g. type: postgres) or type: memory"
        ),
    }

    def build_executor(self, config: DuckDBConnectionConfig, context: AdapterContext) -> Any:
        from metric_runtime.adapters.duckdb.executor import DuckDBExecutor

        if config.path is None:
            raise ConfigurationError("DuckDB connection requires path")
        path = Path(config.path)
        if not path.is_absolute():
            path = (context.base_dir / path).resolve()
        return DuckDBExecutor(
            path,
            fact_table=config.fact_table or context.project.runtime.fact_table,
            read_only=config.read_only,
        )
