"""Shared batch calculation sources (execution optimization)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from metric_runtime.calculations.context import EvaluationContext
from metric_runtime.exceptions import MetricRuntimeError, UnknownMetricError


@runtime_checkable
class BatchSource(Protocol):
    """Produces many named numeric results for one evaluation context."""

    name: str

    def execute(self, context: EvaluationContext) -> dict[str, float | None]: ...


class SqlBatchSource:
    """SQL text that returns one row with multiple named KPI columns."""

    def __init__(
        self,
        name: str,
        query: str,
        *,
        dialect: str | None = None,
        columns: tuple[str, ...] | None = None,
        bindings: dict[str, object] | None = None,
    ) -> None:
        from metric_runtime.calculations.specs import _validate_scalar_bindings

        self.name = name
        self.query = query.strip()
        self.dialect = dialect
        self.columns = columns
        self.bindings = _validate_scalar_bindings(dict(bindings or {}))
        if not self.query:
            raise ValueError("SqlBatchSource.query must be non-empty")


class CallableBatchSource:
    """Application-registered Python handler (not YAML-loaded)."""

    def __init__(
        self,
        name: str,
        handler: Callable[[EvaluationContext], dict[str, float | None]],
    ) -> None:
        self.name = name
        self._handler = handler

    def execute(self, context: EvaluationContext) -> dict[str, float | None]:
        result = self._handler(context)
        if not isinstance(result, dict):
            raise MetricRuntimeError(
                f"Batch source {self.name!r} must return dict[str, float | None]"
            )
        return {str(k): (None if v is None else float(v)) for k, v in result.items()}


class BatchRegistry:
    """Application-owned registry of shared batch sources."""

    def __init__(self) -> None:
        self._sources: dict[str, BatchSource | SqlBatchSource | CallableBatchSource] = {}

    def register(
        self,
        name: str,
        source: BatchSource | SqlBatchSource | CallableBatchSource,
    ) -> None:
        key = name.strip()
        if not key:
            raise ValueError("batch source name must be non-empty")
        if key in self._sources:
            raise MetricRuntimeError(f"Batch source already registered: {key!r}")
        # Allow registering bare SqlBatchSource under an explicit name.
        if isinstance(source, SqlBatchSource) and source.name != key:
            source = SqlBatchSource(
                key,
                source.query,
                dialect=source.dialect,
                columns=source.columns,
                bindings=source.bindings,
            )
        elif isinstance(source, CallableBatchSource) and source.name != key:
            source = CallableBatchSource(key, source._handler)
        self._sources[key] = source

    def get(self, name: str) -> BatchSource | SqlBatchSource | CallableBatchSource:
        try:
            return self._sources[name]
        except KeyError as exc:
            raise UnknownMetricError(f"Unknown batch source: {name!r}") from exc

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._sources

    def names(self) -> list[str]:
        return list(self._sources)
