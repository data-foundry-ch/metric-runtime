"""Semantic models for executable business metrics."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
)

from metric_runtime.detectors.policy import SeasonalZScore, Threshold
from metric_runtime.identity import EvaluationKey, parse_datetime

_MEASURE_REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def coerce_measure_ref(value: Any) -> str:
    """Normalize enums / strings into a generic measure identifier."""
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str):
        raise TypeError(f"Measure reference must be a string, got {type(value)!r}")
    name = value.strip()
    if not _MEASURE_REF_RE.match(name):
        raise ValueError(
            f"Invalid measure reference {name!r}. "
            "Expected an identifier like 'orders' or 'gross_revenue'."
        )
    return name


# Public alias: validated measure identifier (not a framework-owned domain enum).
MeasureRef = str


class Directionality(str, Enum):
    LOWER_IS_BAD = "lower_is_bad"
    HIGHER_IS_BAD = "higher_is_bad"
    TWO_SIDED = "two_sided"


class KPIState(str, Enum):
    """Operational state of a metric.

    Observation != Detection != State != Incident.
    """

    NORMAL = "NORMAL"
    DETECTED = "DETECTED"
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    SUPPRESSED = "SUPPRESSED"


# Back-compat alias used by older call sites / talk material.
MetricState = KPIState


class Formula(BaseModel):
    """How a KPI is calculated from generic measure references."""

    kind: Literal["sum", "ratio", "difference"]
    measure: MeasureRef | None = None
    numerator: MeasureRef | None = None
    denominator: MeasureRef | None = None
    left: MeasureRef | None = None
    right: MeasureRef | None = None

    @field_validator("measure", "numerator", "denominator", "left", "right", mode="before")
    @classmethod
    def _coerce_refs(cls, value: Any) -> Any:
        if value is None:
            return None
        return coerce_measure_ref(value)

    @model_validator(mode="after")
    def validate_shape(self) -> Formula:
        if self.kind == "sum" and self.measure is None:
            raise ValueError("sum formulas require measure")
        if self.kind == "ratio" and (self.numerator is None or self.denominator is None):
            raise ValueError("ratio formulas require numerator and denominator")
        if self.kind == "difference" and (self.left is None or self.right is None):
            raise ValueError("difference formulas require left and right")
        return self

    @classmethod
    def sum(cls, measure: str | Enum) -> Formula:
        return cls(kind="sum", measure=coerce_measure_ref(measure))

    @classmethod
    def ratio(cls, numerator: str | Enum, denominator: str | Enum) -> Formula:
        return cls(
            kind="ratio",
            numerator=coerce_measure_ref(numerator),
            denominator=coerce_measure_ref(denominator),
        )

    @classmethod
    def difference(cls, left: str | Enum, right: str | Enum) -> Formula:
        return cls(
            kind="difference",
            left=coerce_measure_ref(left),
            right=coerce_measure_ref(right),
        )


class DetectorConfig(BaseModel):
    """Runtime parameters consumed by detector implementations.

    Prefer attaching a serializable detector spec on the KPI
    (``SeasonalZScore`` / ``Threshold``). ``DetectorConfig`` remains the
    parameter bag passed into ``DetectorStrategy.evaluate``.
    """

    baseline_weeks: int = Field(default=6, ge=3, le=12)
    z_threshold: float = Field(default=2.5, gt=0)
    min_relative_change: float = Field(default=0.08, ge=0)
    absolute_threshold: float | None = None


class SupportRequirement(BaseModel):
    measure: MeasureRef
    minimum: float = Field(default=30.0, ge=0)

    @field_validator("measure", mode="before")
    @classmethod
    def _coerce_measure(cls, value: Any) -> str:
        return coerce_measure_ref(value)


class ImpactModel(BaseModel):
    """How estimated impact is derived from value vs baseline.

    ``quantity_delta`` multiplies a lost/gained quantity by another metric's
    unit value (``unit_value_metric``). Core does not hardcode domain metrics.
    """

    kind: Literal[
        "none",
        "margin_delta",
        "revenue_delta",
        "cost_delta",
        "quantity_delta",
    ] = "none"
    unit_value_metric: str | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _alias_orders_delta(cls, value: Any) -> Any:
        if value == "orders_delta":
            return "quantity_delta"
        return value


def _default_detector_spec() -> SeasonalZScore:
    return SeasonalZScore()


class KPI(BaseModel):
    """Executable semantic object for a business metric.

    A KPI says what the metric means. The runtime profile says where it
    is evaluated.

    Presentation / layout hints belong in ``metadata`` (or the example
    layer), not as first-class core fields.
    """

    name: str
    label: str | None = None
    description: str = ""
    owner: str = ""
    formula: Formula | None = None
    dimensions: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    directionality: Directionality = Directionality.TWO_SIDED
    detector: Annotated[SeasonalZScore | Threshold, Field(discriminator="type")] = Field(
        default_factory=_default_detector_spec
    )
    support: SupportRequirement | None = None
    impact: ImpactModel = Field(default_factory=ImpactModel)
    unit: Literal["count", "ratio", "eur", "percent", "unit"] = "unit"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("detector", mode="before")
    @classmethod
    def _coerce_detector(cls, value: Any) -> Any:
        from metric_runtime.detectors.policy import coerce_detector_spec

        return coerce_detector_spec(value)

    @model_validator(mode="after")
    def _defaults(self) -> KPI:
        if self.label is None:
            object.__setattr__(self, "label", self.name.replace("_", " ").title())
        return self

    @property
    def display_name(self) -> str:
        return self.label or self.name

    def presentation(self) -> dict[str, Any]:
        """Optional example/presentation hints stored under metadata."""
        raw = self.metadata.get("presentation")
        return dict(raw) if isinstance(raw, dict) else {}


# Back-compat alias.
KPIDefinition = KPI


class KPIObservation(BaseModel):
    """A measured value at a point in time / scope.

    Detection asks whether something is unusual.
    An observation alone is not an alert.
    """

    name: str
    value: float
    support: float = 0.0
    support_ok: bool = True
    as_of: datetime
    filters: dict[str, str] = Field(default_factory=dict)
    baseline_values: list[float] = Field(default_factory=list)

    @field_validator("as_of", mode="before")
    @classmethod
    def _coerce_as_of(cls, value: Any) -> datetime:
        return parse_datetime(value)


class Detection(BaseModel):
    """Detector output for one observation."""

    name: str
    anomalous: bool
    z_score: float = 0.0
    relative_change: float = 0.0
    baseline_mean: float = 0.0
    baseline_std: float = 0.0
    severity: float = 0.0
    state: KPIState = KPIState.NORMAL
    details: dict[str, Any] = Field(default_factory=dict)


class KPIStatus(BaseModel):
    """Convenience evaluate result: observation + detection together.

    Prefer thinking in Observation / Detection / State / Incident terms.
    KPIStatus remains the practical return type of ``KPIEngine.evaluate``.
    """

    name: str
    value: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    relative_change: float
    anomaly: bool
    support: float
    support_ok: bool
    as_of: datetime
    directionality: Directionality = Directionality.TWO_SIDED
    root_candidate: bool = False
    state: KPIState = KPIState.NORMAL
    severity: float = 0.0

    @field_validator("as_of", mode="before")
    @classmethod
    def _coerce_as_of(cls, value: Any) -> datetime:
        return parse_datetime(value)

    def to_observation(self, filters: dict[str, str] | None = None) -> KPIObservation:
        return KPIObservation(
            name=self.name,
            value=self.value,
            support=self.support,
            support_ok=self.support_ok,
            as_of=self.as_of,
            filters=dict(filters or {}),
        )

    def to_detection(self) -> Detection:
        return Detection(
            name=self.name,
            anomalous=self.anomaly,
            z_score=self.z_score,
            relative_change=self.relative_change,
            baseline_mean=self.baseline_mean,
            baseline_std=self.baseline_std,
            severity=self.severity,
            state=self.state,
        )


class StoredObservation(BaseModel):
    """Persisted observation for one EvaluationKey."""

    key: EvaluationKey
    status: KPIStatus
    eligible_for_state: bool = True
    quality_healthy: bool | None = None
    recorded_at: datetime

    @field_validator("recorded_at", mode="before")
    @classmethod
    def _coerce_recorded_at(cls, value: Any) -> datetime:
        return parse_datetime(value)


class MetricStateRecord(BaseModel):
    """Operational state for one metric + scope."""

    metric: str
    scope_key: str = ""
    scope: dict[str, str] = Field(default_factory=dict)
    state: KPIState = KPIState.NORMAL
    state_since: datetime
    updated_at: datetime
    opened_at: datetime | None = None
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    detection_streak: int = 0
    healthy_streak: int = 0

    @field_validator(
        "state_since",
        "updated_at",
        "opened_at",
        "acknowledged_at",
        "resolved_at",
        mode="before",
    )
    @classmethod
    def _coerce_dt(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        return parse_datetime(value)


class KPIStateTransition(BaseModel):
    """Previous → current operational state for one committed evaluation."""

    previous: KPIState
    current: KPIState

    def __iter__(self):
        yield self.previous
        yield self.current

    def __eq__(self, other: object) -> bool:
        if isinstance(other, KPIStateTransition):
            return self.previous == other.previous and self.current == other.current
        if isinstance(other, tuple) and len(other) == 2:
            return self.previous == other[0] and self.current == other[1]
        return NotImplemented


class DrilldownRow(BaseModel):
    filters: dict[str, str]
    value: float
    baseline_mean: float
    relative_change: float
    z_score: float
    anomaly: bool
    support: float
    impact_eur: float


class ExplanatoryCandidate(BaseModel):
    name: str
    owner: str
    depth: int
    status: KPIStatus
    impact_eur: float
    score: float


class InvestigationResult(BaseModel):
    """Structured graph-aware investigation result.

    Root candidates and explanatory paths are hypotheses supported by
    the semantic graph — not proven causality.
    """

    metric: str
    anomalous_metrics: list[KPIStatus]
    normal_dependencies: list[str]
    root_candidates: list[KPIStatus]
    explanatory_paths: list[list[str]]
    deepest_candidates: list[ExplanatoryCandidate]
    primary_explanatory: ExplanatoryCandidate | None = None
    impact_eur: float = 0.0

    @property
    def start_metric(self) -> str:
        return self.metric

    @property
    def anomalous(self) -> list[KPIStatus]:
        return self.anomalous_metrics


class DimensionStep(BaseModel):
    scope: dict[str, str]
    value: float
    baseline_mean: float
    relative_change: float
    z_score: float
    anomaly: bool
    support: float
    impact_eur: float


class QualityReport(BaseModel):
    healthy: bool
    freshness_ok: bool
    completeness_ok: bool
    volume_ok: bool
    row_count: int
    missing_rate: float
    latest_ts: str
    message: str


class IncidentState(str, Enum):
    NORMAL = "NORMAL"
    DETECTED = "DETECTED"
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    SUPPRESSED = "SUPPRESSED"


class Incident(BaseModel):
    id: str | None = None
    primary_metric: str
    explanatory_kpi: str
    root_candidates: list[str] = Field(default_factory=list)
    scope: dict[str, str] = Field(default_factory=dict)
    owner: str
    state: IncidentState
    opened_at: datetime | None = None
    updated_at: datetime | None = None
    first_detected: datetime
    estimated_impact: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    related_metrics: list[str] = Field(default_factory=list)
    supporting_metrics: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)
    persistence_windows: int = 1
    suppressed_ancestors: list[str] = Field(default_factory=list)

    @field_validator("opened_at", "updated_at", "first_detected", mode="before")
    @classmethod
    def _coerce_dt(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        return parse_datetime(value)

    @property
    def kpi(self) -> str:
        return self.primary_metric

    @property
    def impact_eur(self) -> float:
        return self.estimated_impact


class OutboxEvent(BaseModel):
    """Durable notification intent — persist before delivery.

    Runtime commits create at most one outbox intent per meaningful transition
    (``event_key``). External delivery is at-least-once unless the notifier
    honors ``event_key`` as an idempotency key.
    """

    id: str | None = None
    event_key: str = ""
    kind: Literal["incident_opened", "incident_updated", "incident_resolved", "state_changed"]
    metric: str = ""
    incident_id: str | None = None
    incident: Incident | None = None
    previous_state: KPIState | None = None
    current_state: KPIState | None = None
    message: str = ""
    created_at: datetime
    delivered_at: datetime | None = None
    attempt_count: int = 0
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    next_attempt_at: datetime | None = None

    @field_validator(
        "created_at",
        "delivered_at",
        "last_attempt_at",
        "next_attempt_at",
        mode="before",
    )
    @classmethod
    def _coerce_dt(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        return parse_datetime(value)

    @property
    def pending(self) -> bool:
        return self.delivered_at is None


# Back-compat alias used by ProcessResult / older call sites.
NotificationEvent = OutboxEvent


class EvaluationRecord(BaseModel):
    """Authoritative committed result for one EvaluationKey."""

    key: EvaluationKey
    observation: StoredObservation
    transition: KPIStateTransition
    status: KPIStatus
    state_record: MetricStateRecord
    incident_ids: list[str] = Field(default_factory=list)
    outbox_event_ids: list[str] = Field(default_factory=list)
    new_incident_ids: list[str] = Field(default_factory=list)
    updated_incident_ids: list[str] = Field(default_factory=list)
    investigation: InvestigationResult | None = None
    committed_at: datetime

    @field_validator("committed_at", mode="before")
    @classmethod
    def _coerce_committed_at(cls, value: Any) -> datetime:
        return parse_datetime(value)


class ProcessResult(BaseModel):
    """Outcome of one authoritative runtime tick."""

    metric: str
    scope: dict[str, str] = Field(default_factory=dict)
    evaluation_key: EvaluationKey | None = None
    at: datetime
    status: KPIStatus
    transition: KPIStateTransition
    new_incidents: list[Incident] = Field(default_factory=list)
    updated_incidents: list[Incident] = Field(default_factory=list)
    notifications: list[OutboxEvent] = Field(default_factory=list)
    investigation: InvestigationResult | None = None
    idempotent: bool = False
    evaluation_record: EvaluationRecord | None = None

    @field_validator("at", mode="before")
    @classmethod
    def _coerce_at(cls, value: Any) -> datetime:
        return parse_datetime(value)
