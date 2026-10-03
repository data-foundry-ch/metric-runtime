"""Semantic models for executable business metrics."""

from __future__ import annotations

import hashlib
import json
import warnings
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from metric_runtime.calculations.specs import (
    BatchCalculation,
    DerivedCalculation,
    FormulaCalculation,
    SqlCalculation,
    coerce_calculation,
)
from metric_runtime.detectors.policy import SeasonalZScore, Threshold
from metric_runtime.identity import EvaluationKey, parse_datetime
from metric_runtime.ids import MetricId, validate_metric_id
from metric_runtime.measures import Formula, MeasureRef, coerce_measure_ref
from metric_runtime.units import UnitSpec, coerce_unit


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


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _assert_json_safe(value: Any, *, path: str = "metadata") -> Any:
    """Reject values that are not strict JSON scalars/containers."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError(f"{path} contains non-JSON float {value!r}")
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings, got {type(key)!r}")
            out[key] = _assert_json_safe(item, path=f"{path}.{key}")
        return out
    if isinstance(value, list):
        return [_assert_json_safe(item, path=f"{path}[]") for item in value]
    raise ValueError(
        f"{path} must be JSON-serializable (str|int|float|bool|None|list|dict), got {type(value)!r}"
    )


class Metric(BaseModel):
    """Canonical semantic metric definition.

    ``id`` is stable machine identity (dependencies, state, observations).
    ``name`` is the human-facing display label.

    Product workflow fields (draft/publish, layout, tenancy, RBAC) do **not**
    belong here — wrap ``Metric`` in a product model instead.
    """

    model_config = ConfigDict(frozen=True)

    id: MetricId
    name: str = ""
    description: str = ""
    owner: str = ""
    calculation: Annotated[
        FormulaCalculation | SqlCalculation | BatchCalculation | DerivedCalculation,
        Field(discriminator="kind"),
    ]
    formula: Formula | None = Field(
        default=None,
        exclude=True,
        description="Authoring sugar for FormulaCalculation; excluded from serialization.",
    )
    dimensions: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    directionality: Directionality = Directionality.TWO_SIDED
    detector: Annotated[SeasonalZScore | Threshold, Field(discriminator="type")] = Field(
        default_factory=_default_detector_spec
    )
    support: SupportRequirement | None = None
    impact: ImpactModel = Field(default_factory=ImpactModel)
    unit: UnitSpec = Field(default_factory=lambda: UnitSpec(id="unit"))
    format: str | None = Field(
        default=None,
        description="Optional display format hint for embedders (e.g. '0.0%', '#,##0').",
    )
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_identity(cls, data: Any) -> Any:
        """Accept legacy KPI JSON where ``name`` was the machine id."""
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if "id" not in payload and "name" in payload:
            candidate = payload.get("name")
            if isinstance(candidate, str):
                try:
                    validate_metric_id(candidate)
                except Exception:  # noqa: BLE001
                    pass
                else:
                    payload["id"] = payload.pop("name")
                    if "label" in payload and not payload.get("name"):
                        payload["name"] = payload.pop("label")
                    else:
                        payload.pop("label", None)
        elif "label" in payload:
            label = payload.pop("label")
            if not payload.get("name"):
                payload["name"] = label

        metric_id = payload.get("id")
        if not payload.get("name") and isinstance(metric_id, str) and metric_id:
            payload["name"] = metric_id.replace("_", " ").title()

        # formula= sugar → FormulaCalculation when calculation omitted.
        if payload.get("calculation") is None and payload.get("formula") is not None:
            payload["calculation"] = FormulaCalculation(formula=payload["formula"])
        elif payload.get("calculation") is None:
            raise ValueError("Metric requires calculation= (or formula= sugar)")

        # Keep in-memory formula mirror for FormulaCalculation (not serialized).
        calc = payload.get("calculation")
        if payload.get("formula") is None:
            if isinstance(calc, FormulaCalculation):
                payload["formula"] = calc.formula
            elif isinstance(calc, dict) and calc.get("kind", "formula") == "formula":
                if "formula" in calc:
                    payload["formula"] = calc["formula"]
        return payload

    @field_validator("unit", mode="before")
    @classmethod
    def _coerce_unit(cls, value: Any) -> Any:
        return coerce_unit(value)

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (set, frozenset, tuple, list)):
            return sorted({str(item).strip() for item in value if str(item).strip()})
        raise TypeError("tags must be a list/set of strings")

    @field_validator("dependencies", "dimensions", mode="before")
    @classmethod
    def _tuple_ids(cls, value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(value)

    @field_validator("dependencies")
    @classmethod
    def _validate_dependency_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_metric_id(item) for item in value)

    @field_validator("detector", mode="before")
    @classmethod
    def _coerce_detector(cls, value: Any) -> Any:
        from metric_runtime.detectors.policy import coerce_detector_spec

        return coerce_detector_spec(value)

    @field_validator("calculation", mode="before")
    @classmethod
    def _coerce_calculation(cls, value: Any) -> Any:
        return coerce_calculation(value)

    @field_validator("metadata")
    @classmethod
    def _json_safe_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("metadata must be a dict")
        return _assert_json_safe(value)

    @model_validator(mode="after")
    def _check_formula_alignment(self) -> Metric:
        if isinstance(self.calculation, FormulaCalculation):
            if self.formula is not None and self.formula != self.calculation.formula:
                raise ValueError(f"Metric {self.id!r}: formula and calculation.formula disagree")
        elif self.formula is not None:
            raise ValueError(f"Metric {self.id!r}: formula= is only valid with FormulaCalculation")
        return self

    @property
    def display_name(self) -> str:
        """Human-facing label (``name``)."""
        return self.name or self.id

    @property
    def label(self) -> str:
        """Deprecated alias for display ``name`` (KPI compatibility)."""
        return self.display_name

    def display(self) -> dict[str, Any]:
        """Product display hints: name / unit / format (not layout)."""
        return {
            "label": self.display_name,
            "name": self.display_name,
            "unit": self.unit.model_dump(mode="json"),
            "format": self.format,
        }

    def presentation(self) -> dict[str, Any]:
        """Optional layout/example hints stored under metadata['presentation'].

        Prefer ``display()`` for name/unit/format. Keep graph parent/ring/order
        in metadata — not first-class core fields.
        """
        raw = self.metadata.get("presentation")
        return dict(raw) if isinstance(raw, dict) else {}

    def semantic_payload(self) -> dict[str, Any]:
        """Fields that affect executable / routing semantics."""
        return {
            "id": self.id,
            "calculation": self.calculation.model_dump(mode="json"),
            "dependencies": list(self.dependencies),
            "dimensions": list(self.dimensions),
            "unit": self.unit.model_dump(mode="json"),
            "detector": self.detector.model_dump(mode="json"),
            "directionality": self.directionality.value,
            "support": self.support.model_dump(mode="json") if self.support else None,
            "impact": self.impact.model_dump(mode="json"),
            "owner": self.owner,
        }

    def semantic_hash(self) -> str:
        """SHA-256 of executable semantics (excludes display name/description/tags)."""
        return hashlib.sha256(_canonical_json_bytes(self.semantic_payload())).hexdigest()

    def content_hash(self) -> str:
        """SHA-256 of the full metric document (including display fields)."""
        return hashlib.sha256(_canonical_json_bytes(self.model_dump(mode="json"))).hexdigest()


class KPI(Metric):
    """Deprecated compatibility alias for :class:`Metric`.

    Legacy authoring used ``name`` as machine id and ``label`` as display name.
    Prefer::

        Metric(id=\"profit_margin\", name=\"Profit Margin\", ...)
    """

    @model_validator(mode="before")
    @classmethod
    def _warn_deprecated(cls, data: Any) -> Any:
        warnings.warn(
            "KPI is deprecated; use Metric(id=..., name=...). "
            "KPI remains a temporary compatibility alias.",
            DeprecationWarning,
            stacklevel=2,
        )
        return data


# Back-compat alias.
KPIDefinition = KPI


class KPIObservation(BaseModel):
    """A measured value at a point in time / scope.

    Detection asks whether something is unusual.
    An observation alone is not an alert.

    ``name`` stores the stable :class:`Metric` id (historical field name).
    Prefer ``metric_id`` for new code.

    Null semantics for embedders / UI bridges:

    - ``value_status == "value"``: ``value`` is a real measurement (including
      legitimate ``0.0``). Use ``measured_value`` / ``has_value``.
    - ``value_status == "no_data"``: no row / NULL / missing deps — ``value``
      is a placeholder ``0.0`` and **must not** be shown as zero.
    - ``value_status == "error"``: calculation failed — see
      ``calculation_error``; ``value`` is again a placeholder ``0.0``.

    Prefer ``measured_value`` (``float | None``) when bridging to nullable UI
    fields so NO_DATA/ERROR do not look like a measured zero.
    """

    name: str
    value: float
    support: float = 0.0
    support_ok: bool = True
    as_of: datetime
    filters: dict[str, str] = Field(default_factory=dict)
    baseline_values: list[float] = Field(default_factory=list)
    value_status: str = "value"
    calculation_error: str | None = None
    metric_definition_hash: str | None = None

    @field_validator("as_of", mode="before")
    @classmethod
    def _coerce_as_of(cls, value: Any) -> datetime:
        return parse_datetime(value)

    @property
    def metric_id(self) -> str:
        """Stable Metric.id (same string as ``name``)."""
        return self.name

    @property
    def has_value(self) -> bool:
        return self.value_status == "value"

    @property
    def measured_value(self) -> float | None:
        """Nullable measurement for UI bridges; ``None`` when NO_DATA/ERROR."""
        return self.value if self.has_value else None

    @property
    def is_no_data(self) -> bool:
        return self.value_status == "no_data"

    @property
    def is_error(self) -> bool:
        return self.value_status == "error"


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
    value_status: str = "value"

    @field_validator("as_of", mode="before")
    @classmethod
    def _coerce_as_of(cls, value: Any) -> datetime:
        return parse_datetime(value)

    @property
    def is_no_data(self) -> bool:
        return self.value_status == "no_data"

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
    value_status: str = "value"
    no_data_reason: str | None = None

    @property
    def is_no_data(self) -> bool:
        return self.value_status == "no_data"

    @field_validator("recorded_at", mode="before")
    @classmethod
    def _coerce_recorded_at(cls, value: Any) -> datetime:
        return parse_datetime(value)


class MetricStateRecord(BaseModel):
    """Operational state for one metric + scope stream.

    Time semantics:
    - ``last_evaluation_at`` / evaluation windows use **effective_at** — the
      business time of the observation window (``EvaluationKey.eval_at``).
    - ``updated_at`` is **processed_at** — when the runtime committed this
      state row (wall clock).
    """

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
    last_evaluation_at: datetime | None = None
    version: int = 0

    @field_validator(
        "state_since",
        "updated_at",
        "opened_at",
        "acknowledged_at",
        "resolved_at",
        "last_evaluation_at",
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
    model_config = ConfigDict(populate_by_name=True)

    filters: dict[str, str]
    value: float
    baseline_mean: float
    relative_change: float
    z_score: float
    anomaly: bool
    support: float
    impact: float = Field(validation_alias=AliasChoices("impact", "impact_eur"))

    @property
    def impact_eur(self) -> float:
        """Deprecated alias for ``impact`` (currency-agnostic)."""
        return self.impact


class ExplanatoryCandidate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    owner: str
    depth: int
    status: KPIStatus
    impact: float = Field(validation_alias=AliasChoices("impact", "impact_eur"))
    score: float

    @property
    def impact_eur(self) -> float:
        return self.impact


class InvestigationResult(BaseModel):
    """Structured graph-aware investigation result.

    Root candidates and explanatory paths are hypotheses supported by
    the semantic graph — not proven causality.
    """

    model_config = ConfigDict(populate_by_name=True)

    metric: str
    anomalous_metrics: list[KPIStatus]
    normal_dependencies: list[str]
    root_candidates: list[KPIStatus]
    explanatory_paths: list[list[str]]
    deepest_candidates: list[ExplanatoryCandidate]
    primary_explanatory: ExplanatoryCandidate | None = None
    impact: float = Field(default=0.0, validation_alias=AliasChoices("impact", "impact_eur"))

    @property
    def impact_eur(self) -> float:
        return self.impact

    @property
    def start_metric(self) -> str:
        return self.metric

    @property
    def anomalous(self) -> list[KPIStatus]:
        return self.anomalous_metrics


class DimensionStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope: dict[str, str]
    value: float
    baseline_mean: float
    relative_change: float
    z_score: float
    anomaly: bool
    support: float
    impact: float = Field(validation_alias=AliasChoices("impact", "impact_eur"))

    @property
    def impact_eur(self) -> float:
        return self.impact


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
    claim_token: str | None = None
    claimed_until: datetime | None = None
    dead_lettered_at: datetime | None = None

    @field_validator(
        "created_at",
        "delivered_at",
        "last_attempt_at",
        "next_attempt_at",
        "claimed_until",
        "dead_lettered_at",
        mode="before",
    )
    @classmethod
    def _coerce_dt(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        return parse_datetime(value)

    @property
    def pending(self) -> bool:
        return self.delivered_at is None and self.dead_lettered_at is None


# Back-compat alias used by ProcessResult / older call sites.
NotificationEvent = OutboxEvent


class EvaluationRecord(BaseModel):
    """Authoritative committed result for one EvaluationKey.

    ``effective_at`` is the evaluation window time (business time).
    ``committed_at`` / ``processed_at`` is when the runtime committed.
    """

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
    effective_at: datetime | None = None

    @field_validator("committed_at", "effective_at", mode="before")
    @classmethod
    def _coerce_committed_at(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        return parse_datetime(value)

    @property
    def processed_at(self) -> datetime:
        return self.committed_at


class ProcessResult(BaseModel):
    """Outcome of one authoritative runtime tick.

    ``at`` / ``effective_at`` is the evaluation window time.
    ``processed_at`` is when the result was committed (if available).
    """

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
    processed_at: datetime | None = None

    @field_validator("at", "processed_at", mode="before")
    @classmethod
    def _coerce_at(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        return parse_datetime(value)

    @property
    def effective_at(self) -> datetime:
        return self.at
