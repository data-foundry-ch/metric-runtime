"""Execution backends.

A KPI definition describes what a KPI means.
An executor knows how to calculate observations from a resource.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from metric_runtime.models import Formula, MeasureRef


@runtime_checkable
class MetricExecutor(Protocol):
    """Knows how to calculate metric observations using a resource."""

    def metric_value(
        self,
        formula: Formula,
        *,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float: ...

    def measure_value(
        self,
        measure: MeasureRef,
        *,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float: ...

    def distinct_groups(
        self,
        dimensions: tuple[str, ...],
        *,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        valid_dimensions: set[str] | None = None,
    ) -> list[dict[str, str]]: ...
