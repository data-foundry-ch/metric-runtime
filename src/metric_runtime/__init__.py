"""metric-runtime — define, validate, execute and operationalize semantic metrics."""

from __future__ import annotations

from metric_runtime.calculations import (
    BatchCalculation,
    BatchRegistry,
    DerivedCalculation,
    EvaluationContext,
    EvaluationSession,
    FormulaCalculation,
    SqlCalculation,
)
from metric_runtime.catalog import (
    KPICatalog,
    MetricCatalog,
    MetricCatalogDiff,
    MetricCatalogSnapshot,
)
from metric_runtime.detectors import (
    SeasonalZScore,
    SeasonalZScoreDetector,
    Threshold,
    ThresholdDetector,
)
from metric_runtime.engine import KPIEngine
from metric_runtime.identity import EvaluationKey
from metric_runtime.ids import MetricId
from metric_runtime.incidents import open_smart_incident
from metric_runtime.investigation import investigate_metric, investigate_window
from metric_runtime.models import (
    KPI,
    Detection,
    Directionality,
    EvaluationRecord,
    Formula,
    Incident,
    InvestigationResult,
    KPIObservation,
    KPIState,
    KPIStatus,
    Metric,
    MetricStateRecord,
    OutboxEvent,
    ProcessResult,
)
from metric_runtime.presentation import (
    PresentationBand,
    PresentationThreshold,
    classify_presentation_band,
)
from metric_runtime.state import KPIStateTransition, StatePolicy
from metric_runtime.stores import InMemoryStateStore
from metric_runtime.units import UnitSpec

__all__ = [
    "Metric",
    "MetricCatalog",
    "MetricCatalogDiff",
    "MetricCatalogSnapshot",
    "MetricId",
    "UnitSpec",
    "KPI",
    "KPICatalog",
    "KPIEngine",
    "KPIObservation",
    "KPIState",
    "KPIStateTransition",
    "KPIStatus",
    "BatchCalculation",
    "BatchRegistry",
    "DerivedCalculation",
    "Detection",
    "EvaluationContext",
    "EvaluationKey",
    "EvaluationRecord",
    "EvaluationSession",
    "FormulaCalculation",
    "Incident",
    "InvestigationResult",
    "MetricStateRecord",
    "OutboxEvent",
    "PresentationBand",
    "PresentationThreshold",
    "ProcessResult",
    "Directionality",
    "Formula",
    "SqlCalculation",
    "SeasonalZScore",
    "SeasonalZScoreDetector",
    "Threshold",
    "ThresholdDetector",
    "InMemoryStateStore",
    "StatePolicy",
    "classify_presentation_band",
    "investigate_metric",
    "investigate_window",
    "open_smart_incident",
]

__version__ = "0.2.0"
