"""Semantic models for executable business metrics."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Measure(str, Enum):
    """Named atomic measures used by formula definitions.

    Measure names are semantic identifiers. How they map to columns or
    expressions is an executor concern.
    """

    SESSIONS = "sessions"
    ORDERS = "orders"
    PROMO_ORDERS = "promo_orders"
    THRESHOLD_BAND_ORDERS = "threshold_band_orders"
    BAND_UNDER_20_ORDERS = "band_under_20_orders"
    BAND_25_29_ORDERS = "band_25_29_orders"
    BAND_30_39_ORDERS = "band_30_39_orders"
    BAND_40_PLUS_ORDERS = "band_40_plus_orders"
    NEW_CUSTOMER_ORDERS = "new_customer_orders"
    LEADS = "leads"
    OPPORTUNITIES = "opportunities"
    RESTAURANT_LEADS = "restaurant_leads"
    GROSS_ORDER_VALUE = "gross_order_value_eur"
    DISCOUNT_COST = "discount_cost_eur"
    DELIVERY_FEE_REVENUE = "delivery_fee_revenue_eur"
    RESTAURANT_PAYOUT = "restaurant_payout_eur"
    DELIVERY_COST = "delivery_cost_eur"
    PAYMENT_PROCESSING_COST = "payment_processing_cost_eur"
    REFUNDS = "refunds_eur"
    NET_REVENUE = "net_revenue_eur"
    VARIABLE_COST = "variable_cost_eur"
    PLATFORM_COST = "platform_cost_eur"
    WEEKEND_PROFIT = "weekend_profit_eur"
    DELIVERY_MINUTES = "delivery_minutes"
    LATE_ORDERS = "late_orders"


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
    kind: Literal["sum", "ratio", "difference"]
    measure: Measure | None = None
    numerator: Measure | None = None
    denominator: Measure | None = None
    left: Measure | None = None
    right: Measure | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Formula:
        if self.kind == "sum" and self.measure is None:
            raise ValueError("sum formulas require measure")
        if self.kind == "ratio" and (self.numerator is None or self.denominator is None):
            raise ValueError("ratio formulas require numerator and denominator")
        if self.kind == "difference" and (self.left is None or self.right is None):
            raise ValueError("difference formulas require left and right")
        return self


class DetectorConfig(BaseModel):
    """Parameters consumed by detector implementations.

    The z-score is one detector implementation, not the architecture.
    """

    baseline_weeks: int = Field(default=6, ge=3, le=12)
    z_threshold: float = Field(default=2.5, gt=0)
    min_relative_change: float = Field(default=0.08, ge=0)
    absolute_threshold: float | None = None


class SupportRequirement(BaseModel):
    measure: Measure
    minimum: float = Field(default=30.0, ge=0)


class ImpactModel(BaseModel):
    kind: Literal[
        "none",
        "margin_delta",
        "revenue_delta",
        "orders_delta",
        "cost_delta",
    ] = "none"


class KPI(BaseModel):
    """Executable semantic object for a business metric.

    A KPI says what the metric means. The runtime profile says where it
    is evaluated.
    """

    name: str
    label: str | None = None
    description: str = ""
    owner: str = ""
    formula: Formula | None = None
    dimensions: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    directionality: Directionality = Directionality.TWO_SIDED
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    support: SupportRequirement | None = None
    impact: ImpactModel = Field(default_factory=ImpactModel)
    unit: Literal["count", "ratio", "eur", "percent", "unit"] = "unit"
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Presentation hints used by examples; ignored by core investigation.
    graph_ring: int = 0
    graph_side: Literal["center", "marketing", "finance", "operations"] | None = None
    graph_directionality: Directionality | Literal["neutral"] | None = None

    @model_validator(mode="after")
    def _defaults(self) -> KPI:
        if self.label is None:
            object.__setattr__(self, "label", self.name.replace("_", " ").title())
        return self

    @property
    def display_name(self) -> str:
        return self.label or self.name


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
    as_of: str
    filters: dict[str, str] = Field(default_factory=dict)
    baseline_values: list[float] = Field(default_factory=list)


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
    as_of: str
    directionality: Directionality = Directionality.TWO_SIDED
    root_candidate: bool = False
    state: KPIState = KPIState.NORMAL
    severity: float = 0.0

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

    # Back-compat field names used by the PyPizza demo.
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
    opened_at: str | None = None
    updated_at: str | None = None
    first_detected: str
    estimated_impact: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    related_metrics: list[str] = Field(default_factory=list)
    supporting_metrics: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)
    persistence_windows: int = 1
    suppressed_ancestors: list[str] = Field(default_factory=list)

    # Back-compat aliases used by existing demo code.
    @property
    def kpi(self) -> str:
        return self.primary_metric

    @property
    def impact_eur(self) -> float:
        return self.estimated_impact
