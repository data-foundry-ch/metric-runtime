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
)
from metric_runtime.stores import InMemoryStateStore

__all__ = [
    "KPI",
    "KPICatalog",
    "KPIEngine",
    "KPIObservation",
    "KPIState",
    "KPIStatus",
    "Detection",
    "Incident",
    "InvestigationResult",
    "Directionality",
    "Formula",
    "SeasonalZScore",
    "SeasonalZScoreDetector",
    "Threshold",
    "ThresholdDetector",
    "InMemoryStateStore",
    "investigate_metric",
    "investigate_window",
    "open_smart_incident",
]

__version__ = "0.1.0"
