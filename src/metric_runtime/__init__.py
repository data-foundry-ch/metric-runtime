"""metric-runtime — executable semantics for business metrics."""

from __future__ import annotations

from metric_runtime.catalog import KPICatalog
from metric_runtime.detectors import (
    SeasonalZScore,
    SeasonalZScoreDetector,
    Threshold,
    ThresholdDetector,
)
from metric_runtime.engine import KPIEngine
from metric_runtime.identity import EvaluationKey
from metric_runtime.incidents import open_smart_incident
from metric_runtime.investigation import investigate_metric, investigate_window
from metric_runtime.models import (
    KPI,
    Detection,
    Directionality,
    Formula,
    Incident,
    InvestigationResult,
    KPIObservation,
    KPIState,
    KPIStatus,
    MetricStateRecord,
    ProcessResult,
)
from metric_runtime.state import KPIStateTransition, StatePolicy
from metric_runtime.stores import InMemoryStateStore

__all__ = [
    "KPI",
    "KPICatalog",
    "KPIEngine",
    "KPIObservation",
    "KPIState",
    "KPIStateTransition",
    "KPIStatus",
    "Detection",
    "EvaluationKey",
    "Incident",
    "InvestigationResult",
    "MetricStateRecord",
    "ProcessResult",
    "Directionality",
    "Formula",
    "SeasonalZScore",
    "SeasonalZScoreDetector",
    "Threshold",
    "ThresholdDetector",
    "InMemoryStateStore",
    "StatePolicy",
    "investigate_metric",
    "investigate_window",
    "open_smart_incident",
]

__version__ = "0.1.0"
