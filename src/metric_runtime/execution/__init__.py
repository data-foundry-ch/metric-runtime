"""Execution backends."""

from metric_runtime.execution.base import MetricExecutor
from metric_runtime.execution.duckdb import DuckDBExecutor

__all__ = ["MetricExecutor", "DuckDBExecutor"]
