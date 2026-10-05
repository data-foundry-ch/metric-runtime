"""DuckDB adapter: local analytical source (``pip install "metric-runtime[duckdb]"``)."""

from metric_runtime.adapters.duckdb.adapter import DuckDBAdapter
from metric_runtime.adapters.duckdb.config import DuckDBConnectionConfig

__all__ = ["DuckDBAdapter", "DuckDBConnectionConfig"]
