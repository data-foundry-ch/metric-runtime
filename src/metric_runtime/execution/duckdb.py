"""Back-compat location — see :mod:`metric_runtime.adapters.duckdb.executor`."""

from metric_runtime.adapters.duckdb.executor import DuckDBExecutor
from metric_runtime.execution.sql import quote_identifier, quote_relation

__all__ = ["DuckDBExecutor", "quote_identifier", "quote_relation"]
