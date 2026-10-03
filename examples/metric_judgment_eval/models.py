"""Typed contracts for the metric-state judgment experiment.

Provider input is ``JudgmentContext``: snapshot + operational proposal, with
optional deterministic ``GraphAnalysis`` in enriched mode. Benchmark metadata
(``case_id``, notes, accepted answers) must never enter the prompt.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROVIDER_SNAPSHOT_KEYS = frozenset({"focal_metric_ids", "metrics", "dependencies"})
PROVIDER_CONTEXT_KEYS = frozenset({"snapshot", "proposal", "graph_analysis"})
BENCHMARK_METADATA_FIELDS = frozenset(
    {
        "case_id",
        "expected",
        "notes",
        "category",
        "evaluator_notes",
        "accepted_dispositions",
        "why_it_exists",
        "force_model",
    }
)
JUDGMENT_FIELDS = (
    "evidence_sufficient",
    "human_review_required",
    "disposition",
)
INPUT_MODES = ("realistic", "anonymized", "shuffled", "noise")
INPUT_LEVELS = ("raw", "enriched")
InputMode = Literal["realistic", "anonymized", "shuffled", "noise"]
InputLevel = Literal["raw", "enriched"]
ProviderName = Literal["jev", "openai", "fixture"]


def canonical_json_bytes(payload: Any) -> bytes:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def canonical_json(payload: Any, *, indent: int | None = None) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    if indent is None:
        return canonical_json_bytes(payload).decode("utf-8")
    return json.dumps(payload, sort_keys=True, indent=indent, default=str)


def sha256_payload(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _remap_id(metric_id: str | None, mapping: dict[str, str] | None) -> str | None:
    if metric_id is None or mapping is None:
        return metric_id
    return mapping.get(metric_id, metric_id)


def _remap_ids(values: tuple[str, ...], mapping: dict[str, str] | None) -> tuple[str, ...]:
    if mapping is None:
        return values
    return tuple(mapping.get(item, item) for item in values)


class MetricNodeSnapshot(BaseModel):
    """One metric's runtime facts. No narrative, no case labels."""

    model_config = ConfigDict(extra="forbid")

    metric_id: str
    name: str | None = None
    unit: str | None = None
    current_value: float | None = None
    baseline_value: float | None = None
    previous_value: float | None = None
    absolute_change: float | None = None
    relative_change: float | None = None
    state: str
    quality: str | None = None


class DependencyEdge(BaseModel):
    """``metric_id`` semantically depends on ``depends_on``."""

    model_config = ConfigDict(extra="forbid")

    metric_id: str
    depends_on: str


class MetricStateGraphSnapshot(BaseModel):
    """Canonical metric slice: values, states, and dependency edges."""

    model_config = ConfigDict(extra="forbid")

    focal_metric_ids: list[str]
    metrics: list[MetricNodeSnapshot]
    dependencies: list[DependencyEdge]

    @model_validator(mode="after")
    def _canonicalize_order(self) -> MetricStateGraphSnapshot:
        focals = sorted(dict.fromkeys(self.focal_metric_ids))
        metrics = sorted(self.metrics, key=lambda item: item.metric_id)
        edges = sorted(self.dependencies, key=lambda item: (item.metric_id, item.depends_on))
        object.__setattr__(self, "focal_metric_ids", focals)
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "dependencies", edges)
        return self

    def metric_map(self) -> dict[str, MetricNodeSnapshot]:
        return {item.metric_id: item for item in self.metrics}

    def canonical_json(self, *, indent: int | None = None) -> str:
        return canonical_json(self, indent=indent)

    def sha256(self) -> str:
        return sha256_payload(self)

    def provider_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ActivePath(BaseModel):
    """Ordered walk from a focal metric toward an active descendant."""

    model_config = ConfigDict(extra="forbid")

    metric_ids: tuple[str, ...]


class InvestigationCandidate(BaseModel):
    """An active descendant the runtime selected as an investigation candidate."""

    model_config = ConfigDict(extra="forbid")

    metric_id: str
    depth: int
    state: str
    relative_change: float | None = None
    absolute_change: float | None = None
    terminal_active: bool

    def remapped(self, mapping: dict[str, str] | None) -> InvestigationCandidate:
        if mapping is None:
            return self
        return self.model_copy(update={"metric_id": mapping.get(self.metric_id, self.metric_id)})


class GraphAnalysis(BaseModel):
    """Facts Metric Runtime can derive from the dependency graph and states.

    Contains no likely-cause claims, materiality judgments, or review advice.
    """

    model_config = ConfigDict(extra="forbid")

    focal_metric_ids: tuple[str, ...]
    active_direct_dependency_count: int
    active_branch_count: int
    active_paths: tuple[ActivePath, ...]
    max_active_depth: int
    deepest_active_metric_ids: tuple[str, ...]
    investigation_candidates: tuple[InvestigationCandidate, ...]
    shared_active_dependency_ids: tuple[str, ...]
    missing_evidence_metric_ids: tuple[str, ...]
    no_data_metric_ids: tuple[str, ...]
    competing_active_branches: bool
    focal_has_active_dependency: bool
    multiple_focals_share_active_dependency: bool
    directionally_supporting_metric_ids: tuple[str, ...] = ()
    directionally_conflicting_metric_ids: tuple[str, ...] = ()
    directional_support_known: bool = False

    def remapped(self, mapping: dict[str, str] | None) -> GraphAnalysis:
        if mapping is None:
            return self
        return self.model_copy(
            update={
                "focal_metric_ids": _remap_ids(self.focal_metric_ids, mapping),
                "active_paths": tuple(
                    ActivePath(metric_ids=_remap_ids(path.metric_ids, mapping))
                    for path in self.active_paths
                ),
                "deepest_active_metric_ids": _remap_ids(self.deepest_active_metric_ids, mapping),
                "investigation_candidates": tuple(
                    item.remapped(mapping) for item in self.investigation_candidates
                ),
                "shared_active_dependency_ids": _remap_ids(
                    self.shared_active_dependency_ids, mapping
                ),
                "missing_evidence_metric_ids": _remap_ids(
                    self.missing_evidence_metric_ids, mapping
                ),
                "no_data_metric_ids": _remap_ids(self.no_data_metric_ids, mapping),
                "directionally_supporting_metric_ids": _remap_ids(
                    self.directionally_supporting_metric_ids, mapping
                ),
                "directionally_conflicting_metric_ids": _remap_ids(
                    self.directionally_conflicting_metric_ids, mapping
                ),
            }
        )

    def canonical_json(self, *, indent: int | None = None) -> str:
        return canonical_json(self, indent=indent)

    def sha256(self) -> str:
        return sha256_payload(self)


class ProposalKind(str, Enum):
    route_investigation = "route_investigation"
    group_incidents = "group_incidents"
    suppress_redundant_notification = "suppress_redundant_notification"


class OperationalProposal(BaseModel):
    """A concrete operational decision generated from GraphAnalysis facts."""

    model_config = ConfigDict(extra="forbid")

    kind: ProposalKind
    focal_metric_ids: tuple[str, ...]
    target_metric_id: str | None = None
    related_metric_ids: tuple[str, ...] = ()
    rationale_facts: tuple[str, ...] = ()

    def remapped(self, mapping: dict[str, str] | None) -> OperationalProposal:
        if mapping is None:
            return self
        return self.model_copy(
            update={
                "focal_metric_ids": _remap_ids(self.focal_metric_ids, mapping),
                "target_metric_id": _remap_id(self.target_metric_id, mapping),
                "related_metric_ids": _remap_ids(self.related_metric_ids, mapping),
            }
        )

    def canonical_json(self, *, indent: int | None = None) -> str:
        return canonical_json(self, indent=indent)

    def sha256(self) -> str:
        return sha256_payload(self)


class ProposalDisposition(str, Enum):
    accept = "accept"
    reject = "reject"
    request_more_evidence = "request_more_evidence"
    human_review = "human_review"


class ProposalJudgment(BaseModel):
    """Evaluate one proposed operational decision. No free-text explanation."""

    evidence_sufficient: bool = Field(
        description=(
            "Is the supplied deterministic metric and graph evidence sufficient "
            "to apply the proposed operational decision without requesting "
            "additional business evidence?"
        )
    )
    human_review_required: bool = Field(
        description=(
            "Should a human review the proposed operational decision before it is applied?"
        )
    )
    disposition: ProposalDisposition = Field(
        description=(
            "Given only the supplied deterministic evidence, should the "
            "proposed operational decision be accepted, rejected, deferred for "
            "more evidence, or sent for human review?"
        )
    )

    @model_validator(mode="after")
    def _consistent(self) -> ProposalJudgment:
        if self.disposition is ProposalDisposition.accept:
            if not self.evidence_sufficient or self.human_review_required:
                raise ValueError(
                    "accept requires evidence_sufficient=True and human_review_required=False"
                )
        if self.disposition is ProposalDisposition.human_review:
            if not self.human_review_required:
                raise ValueError("human_review requires human_review_required=True")
        if self.disposition is ProposalDisposition.request_more_evidence:
            if self.evidence_sufficient:
                raise ValueError("request_more_evidence requires evidence_sufficient=False")
        return self


class JudgmentContext(BaseModel):
    """Exact object both experimental providers receive for a given run."""

    model_config = ConfigDict(extra="forbid")

    snapshot: MetricStateGraphSnapshot
    proposal: OperationalProposal
    graph_analysis: GraphAnalysis | None = None

    def with_level(self, level: InputLevel) -> JudgmentContext:
        if level == "raw":
            return self.model_copy(update={"graph_analysis": None})
        return self

    def remapped(self, mapping: dict[str, str] | None) -> JudgmentContext:
        analysis = None if self.graph_analysis is None else self.graph_analysis.remapped(mapping)
        return JudgmentContext(
            snapshot=self.snapshot if mapping is None else self.snapshot,
            proposal=self.proposal.remapped(mapping),
            graph_analysis=analysis,
        )

    def provider_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        if self.graph_analysis is None:
            payload.pop("graph_analysis", None)
        return payload

    def canonical_json(self, *, indent: int | None = None) -> str:
        return canonical_json(self.provider_payload(), indent=indent)

    def sha256(self) -> str:
        return sha256_payload(self.provider_payload())


class ProposalExpectation(BaseModel):
    """Evaluator-defined accepted answers for one proposal. Never sent to providers."""

    proposal: OperationalProposal
    accepted_dispositions: set[ProposalDisposition]
    accepted_evidence_sufficient: set[bool]
    accepted_human_review_required: set[bool]
    accepted_candidates: set[str | None] | None = None

    @field_validator(
        "accepted_dispositions",
        "accepted_evidence_sufficient",
        "accepted_human_review_required",
        mode="before",
    )
    @classmethod
    def _coerce_set(cls, value: Any) -> Any:
        if value is None:
            return set()
        if isinstance(value, (str, bool, int, ProposalDisposition)):
            return {value}
        return set(value)

    @field_validator("accepted_candidates", mode="before")
    @classmethod
    def _coerce_candidates(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, bool, int)):
            return {value}
        return set(value)


class BenchmarkCase(BaseModel):
    """One reusable case. Only ``JudgmentContext`` is provider input."""

    case_id: str
    title: str
    category: str
    notes: str
    snapshot: MetricStateGraphSnapshot
    proposals: tuple[ProposalExpectation, ...] = ()
    why_it_exists: str = ""
    force_model: bool = False

    @property
    def evaluator_notes(self) -> str:
        return self.notes

    def primary_expectation(self) -> ProposalExpectation | None:
        return self.proposals[0] if self.proposals else None


class JudgmentRun(BaseModel):
    provider: str
    model: str
    case_id: str
    input_hash: str
    snapshot_hash: str
    proposal_hash: str
    input_mode: InputMode = "realistic"
    input_level: InputLevel = "enriched"
    proposal_kind: str | None = None
    judgment: ProposalJudgment | None = None
    chosen_candidate: str | None = None
    candidate_metric_ids: list[str] = Field(default_factory=list)
    model_required: bool = True
    force_model: bool = False
    skipped_reason: str | None = None
    latency_ms: float | None = None
    provider_details: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    candidate_provider_details: dict[str, Any] | None = None
    candidate_usage: dict[str, Any] | None = None
    error: str | None = None
    simulated: bool = False
    wire_hash: str | None = None
    id_mapping: dict[str, str] | None = None
    validation_ok: bool | None = None


class CaseEvaluation(BaseModel):
    case_id: str
    runs: list[JudgmentRun] = Field(default_factory=list)


class FieldAgreement(BaseModel):
    field: str
    matched: bool
    observed: Any = None
    accepted: list[Any] = Field(default_factory=list)


class RunScore(BaseModel):
    case_id: str
    provider: str
    input_mode: InputMode
    input_level: InputLevel = "enriched"
    field_hits: int
    field_total: int
    candidate_hit: bool | None = None
    fields: list[FieldAgreement] = Field(default_factory=list)
    simulated: bool = False
    skipped: bool = False


class PairwiseAgreement(BaseModel):
    case_id: str
    input_mode: InputMode
    input_level: InputLevel = "enriched"
    field_hits: int
    field_total: int
    candidate_same: bool | None = None
    fields: dict[str, bool] = Field(default_factory=dict)


class ConsistencyStat(BaseModel):
    field: str
    mode: str | None = None
    mode_count: int = 0
    n: int = 0
    consistency: float | None = None


class RobustnessStat(BaseModel):
    test: str
    provider: str
    same_answer: float | None = None
    n: int = 0
    simulated: bool = False
    input_level: InputLevel | None = None
