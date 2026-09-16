"""Typed evaluation request context and calculation results."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from metric_runtime.identity import ensure_utc, parse_datetime

ScalarValue = str | int | float | bool | datetime | None


class ObservationValueStatus(str, Enum):
    """Outcome of a calculation (distinct from detector/state)."""

    VALUE = "value"
    NO_DATA = "no_data"
    ERROR = "error"


class EvaluationContext(BaseModel):
    """Logical evaluation request shared by all calculation kinds.

    ``effective_at`` is business time for the evaluation window.
    Connections / credentials never belong here.
    """

    effective_at: datetime
    window_start: datetime | None = None
    window_end: datetime | None = None
    filters: dict[str, ScalarValue] = Field(default_factory=dict)

    @field_validator("effective_at", "window_start", "window_end", mode="before")
    @classmethod
    def _coerce_dt(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        return parse_datetime(value)

    @model_validator(mode="after")
    def _normalize(self) -> EvaluationContext:
        object.__setattr__(self, "effective_at", ensure_utc(self.effective_at))
        if self.window_start is not None:
            object.__setattr__(self, "window_start", ensure_utc(self.window_start))
        if self.window_end is not None:
            object.__setattr__(self, "window_end", ensure_utc(self.window_end))
        return self

    def bindings(self) -> dict[str, ScalarValue]:
        """Logical parameter bindings for SQL / batch executors.

        Window aliases (same underlying datetimes; ``effective_at`` semantics
        are unchanged):

        - ``effective_at`` / ``at`` / ``as_of_date``
        - ``window_start`` / ``start_date``
        - ``window_end`` / ``end_date``
        """
        start = self.window_start or self.effective_at
        end = self.window_end or self.effective_at
        params: dict[str, ScalarValue] = {
            "effective_at": self.effective_at,
            "window_start": start,
            "window_end": end,
            # Common aliases used in warehouse / product SQL.
            "start_date": start,
            "end_date": end,
            "at": self.effective_at,
            "as_of_date": self.effective_at,
        }
        for key, value in self.filters.items():
            params[str(key)] = value
        return params

    def cache_key(self) -> str:
        """Deterministic key for session-local batch deduplication."""
        import json

        payload = {
            "effective_at": self.effective_at.isoformat(),
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "filters": {str(k): _jsonable(v) for k, v in sorted(self.filters.items())},
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _jsonable(value: ScalarValue) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class CalculationResult(BaseModel):
    """Raw calculation outcome before detector/state processing."""

    metric: str
    status: ObservationValueStatus
    value: float | None = None
    error: str | None = None
    source: str | None = None

    @classmethod
    def from_value(
        cls, metric: str, value: float, *, source: str | None = None
    ) -> CalculationResult:
        return cls(
            metric=metric,
            status=ObservationValueStatus.VALUE,
            value=float(value),
            source=source,
        )

    @classmethod
    def no_data(cls, metric: str, *, source: str | None = None) -> CalculationResult:
        return cls(metric=metric, status=ObservationValueStatus.NO_DATA, source=source)

    @classmethod
    def from_error(cls, metric: str, error: str, *, source: str | None = None) -> CalculationResult:
        return cls(
            metric=metric,
            status=ObservationValueStatus.ERROR,
            error=error,
            source=source,
        )
