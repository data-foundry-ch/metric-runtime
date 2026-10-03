"""Typed evidence packet built from Metric Runtime results.

Judgment models receive this packet. They do not calculate KPIs, walk the
graph, or decide whether a detector fired.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from examples.judgment_comparison.scenarios import (
    FOCAL_METRIC_ID,
    BusinessContext,
    RuntimeSnapshot,
)
from metric_runtime.models import KPIStatus


class MetricEvidence(BaseModel):
    metric_id: str
    name: str
    unit: str | None = None
    current_value: float | None = None
    baseline_value: float | None = None
    absolute_change: float | None = None
    relative_change: float | None = None
    change_pct: float | None = None
    change_percentage_points: float | None = None
    state: str
    anomalous: bool = False
    z_score: float | None = None


class InvestigationEvidence(BaseModel):
    focal_metric_id: str
    evaluated_at: datetime
    runtime_state: str
    context: BusinessContext
    focal_metric: MetricEvidence
    related_metrics: list[MetricEvidence]
    explanatory_path: list[str]
    anomalous_metric_ids: list[str] = Field(default_factory=list)


class CanonicalMetricPacket(BaseModel):
    current_value: float
    baseline_value: float
    change_pct: float | None = None
    change_percentage_points: float | None = None
    state: str
    anomalous: bool


class CanonicalEvidencePacket(BaseModel):
    """One serialized document sent to every judgment provider."""

    incident: dict[str, str]
    context: dict[str, str | None]
    metrics: dict[str, CanonicalMetricPacket]
    investigation: dict[str, Any]


_RATIO_UNITS = {"percent", "ratio"}


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _pct(relative_change: float | None) -> float | None:
    if relative_change is None:
        return None
    return round(float(relative_change) * 100.0, 1)


def metric_evidence_from_status(
    status: KPIStatus,
    *,
    name: str,
    unit: str | None,
    runtime_state: str | None = None,
) -> MetricEvidence:
    current = _round(status.value, 4)
    baseline = _round(status.baseline_mean, 4)
    absolute = None
    points = None
    if current is not None and baseline is not None:
        absolute = _round(current - baseline, 4)
        if unit in _RATIO_UNITS:
            points = _round(current - baseline, 1)
    return MetricEvidence(
        metric_id=status.name,
        name=name,
        unit=unit,
        current_value=current,
        baseline_value=baseline,
        absolute_change=absolute,
        relative_change=_round(status.relative_change, 6),
        change_pct=_pct(status.relative_change),
        change_percentage_points=points,
        state=runtime_state or status.state.value,
        anomalous=bool(status.anomaly),
        z_score=_round(status.z_score, 3),
    )


def build_evidence(snapshot: RuntimeSnapshot) -> InvestigationEvidence:
    statuses = snapshot.statuses
    focal = statuses[FOCAL_METRIC_ID]
    runtime_state = snapshot.process_result.transition.current.value
    related = [
        metric_evidence_from_status(
            statuses[metric_id],
            name=snapshot.metric_names[metric_id],
            unit=snapshot.metric_units[metric_id],
        )
        for metric_id in statuses
        if metric_id != FOCAL_METRIC_ID
    ]
    anomalous = sorted(metric_id for metric_id, status in statuses.items() if status.anomaly)
    return InvestigationEvidence(
        focal_metric_id=FOCAL_METRIC_ID,
        evaluated_at=snapshot.evaluated_at,
        runtime_state=runtime_state,
        context=snapshot.scenario.context,
        focal_metric=metric_evidence_from_status(
            focal,
            name=snapshot.metric_names[FOCAL_METRIC_ID],
            unit=snapshot.metric_units[FOCAL_METRIC_ID],
            runtime_state=runtime_state,
        ),
        related_metrics=related,
        explanatory_path=list(snapshot.explanatory_path),
        anomalous_metric_ids=anomalous,
    )


def _canonical_metric(item: MetricEvidence) -> CanonicalMetricPacket:
    assert item.current_value is not None
    assert item.baseline_value is not None
    return CanonicalMetricPacket(
        current_value=item.current_value,
        baseline_value=item.baseline_value,
        change_pct=item.change_pct,
        change_percentage_points=item.change_percentage_points,
        state=item.state,
        anomalous=item.anomalous,
    )


def to_canonical_packet(evidence: InvestigationEvidence) -> CanonicalEvidencePacket:
    metrics = {evidence.focal_metric.metric_id: _canonical_metric(evidence.focal_metric)}
    for item in evidence.related_metrics:
        metrics[item.metric_id] = _canonical_metric(item)
    context = evidence.context.model_dump(mode="json")
    return CanonicalEvidencePacket(
        incident={
            "metric": evidence.focal_metric_id,
            "state": evidence.runtime_state,
        },
        context={
            "scenario": str(context.get("scenario")),
            "campaign_name": context.get("campaign_name"),
            "campaign_rule": context.get("campaign_rule"),
        },
        metrics=dict(sorted(metrics.items())),
        investigation={"explanatory_path": list(evidence.explanatory_path)},
    )


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


def evidence_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def packet_for_models(evidence: InvestigationEvidence) -> tuple[CanonicalEvidencePacket, str, str]:
    packet = to_canonical_packet(evidence)
    text = canonical_json(packet)
    return packet, text, evidence_hash(packet)
