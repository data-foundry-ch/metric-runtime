"""Execution contract (``MetricExecutor``) and shared SQL helpers.

Platform executors live in adapters (``metric_runtime.adapters.<platform>``);
``DuckDBExecutor`` / ``PostgresExecutor`` stay importable from here.
"""

from __future__ import annotations

from typing import Any

from metric_runtime.execution.base import MetricExecutor

__all__ = ["MetricExecutor", "DuckDBExecutor", "PostgresExecutor"]

_LAZY = {
    "DuckDBExecutor": "metric_runtime.adapters.duckdb.executor",
    "PostgresExecutor": "metric_runtime.adapters.postgres.executor",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
