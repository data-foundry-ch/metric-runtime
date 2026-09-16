"""KPI calculation layer: Formula / SQL / Batch / Derived."""

from __future__ import annotations

from typing import Any

__all__ = [
    "BatchCalculation",
    "BatchRegistry",
    "Calculation",
    "CalculationResult",
    "CallableBatchSource",
    "DerivedCalculation",
    "EvaluationContext",
    "EvaluationPlan",
    "EvaluationSession",
    "FormulaCalculation",
    "ObservationValueStatus",
    "SqlBatchSource",
    "SqlCalculation",
    "parse_calculation",
]


def __getattr__(name: str) -> Any:
    if name in {
        "BatchCalculation",
        "Calculation",
        "DerivedCalculation",
        "FormulaCalculation",
        "SqlCalculation",
        "parse_calculation",
    }:
        from metric_runtime.calculations import specs as _specs

        return getattr(_specs, name)
    if name in {"CalculationResult", "EvaluationContext", "ObservationValueStatus"}:
        from metric_runtime.calculations import context as _context

        return getattr(_context, name)
    if name in {"BatchRegistry", "CallableBatchSource", "SqlBatchSource"}:
        from metric_runtime.calculations import batch as _batch

        return getattr(_batch, name)
    if name in {"EvaluationPlan", "EvaluationSession"}:
        from metric_runtime.calculations import session as _session

        return getattr(_session, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
