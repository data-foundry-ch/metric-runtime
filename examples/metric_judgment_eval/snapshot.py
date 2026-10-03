"""Build canonical metric-state graph snapshots from Metric Runtime objects.

The slice starts at focal metrics, walks semantic dependencies up to a
configurable depth, and copies only facts the runtime already possesses.
No narrative enrichment.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from examples.metric_judgment_eval.models import (
    DependencyEdge,
    MetricNodeSnapshot,
    MetricStateGraphSnapshot,
    sha256_payload,
)
from metric_runtime.catalog import MetricCatalog
from metric_runtime.graph import build_business_graph
from metric_runtime.measures import Formula
from metric_runtime.models import KPIObservation, KPIStatus, Metric, MetricStateRecord

ACTIVE_STATES = frozenset({"OPEN", "DETECTED", "ACKNOWLEDGED"})
NO_DATA_QUALITY = "no_data"


class RuntimeNodeState(BaseModel):
    """Per-metric runtime facts used by the snapshot builder."""

    model_config = ConfigDict(extra="forbid")

    current_value: float | None = None
    baseline_value: float | None = None
    previous_value: float | None = None
    absolute_change: float | None = None
    relative_change: float | None = None
    state: str
    quality: str | None = None


def runtime_state_from_status(
    status: KPIStatus,
    *,
    observation: KPIObservation | None = None,
    record: MetricStateRecord | None = None,
) -> RuntimeNodeState:
    """Adapt public Metric Runtime evaluation objects into node facts."""
    quality = "value"
    if observation is not None:
        quality = observation.value_status
        measured = observation.measured_value
    else:
        measured = status.value
        if status.value == 0.0 and status.baseline_mean == 0.0 and status.support == 0.0:
            # Do not infer NO_DATA from zeros; zeros can be real measurements.
            measured = status.value

    if observation is not None and observation.is_no_data:
        quality = NO_DATA_QUALITY
        measured = None
        baseline = None
        relative = None
        absolute = None
    else:
        baseline = status.baseline_mean
        relative = status.relative_change
        absolute = None
        if measured is not None and baseline is not None:
            absolute = measured - baseline

    state = record.state.value if record is not None else status.state.value
    if quality == NO_DATA_QUALITY:
        state = "NO_DATA"

    return RuntimeNodeState(
        current_value=measured,
        baseline_value=baseline,
        previous_value=None,
        absolute_change=absolute,
        relative_change=relative,
        state=state,
        quality=quality,
    )


def runtime_state_from_statuses(
    statuses: Mapping[str, KPIStatus],
    *,
    observations: Mapping[str, KPIObservation] | None = None,
    records: Mapping[str, MetricStateRecord] | None = None,
) -> dict[str, RuntimeNodeState]:
    observations = observations or {}
    records = records or {}
    return {
        metric_id: runtime_state_from_status(
            status,
            observation=observations.get(metric_id),
            record=records.get(metric_id),
        )
        for metric_id, status in statuses.items()
    }


def _unit_id(metric: Metric) -> str | None:
    unit = metric.unit.id if metric.unit is not None else None
    return unit or None


def _node_snapshot(
    metric: Metric,
    facts: RuntimeNodeState | None,
) -> MetricNodeSnapshot:
    if facts is None:
        return MetricNodeSnapshot(
            metric_id=metric.id,
            name=metric.display_name or metric.id,
            unit=_unit_id(metric),
            current_value=None,
            baseline_value=None,
            previous_value=None,
            absolute_change=None,
            relative_change=None,
            state="NO_DATA",
            quality=NO_DATA_QUALITY,
        )
    return MetricNodeSnapshot(
        metric_id=metric.id,
        name=metric.display_name or metric.id,
        unit=_unit_id(metric),
        current_value=facts.current_value,
        baseline_value=facts.baseline_value,
        previous_value=facts.previous_value,
        absolute_change=facts.absolute_change,
        relative_change=facts.relative_change,
        state=facts.state,
        quality=facts.quality,
    )


def nodes_within_dependency_depth(
    catalog: MetricCatalog,
    focal_metric_ids: Sequence[str],
    *,
    dependency_depth: int,
) -> set[str]:
    """Focal metrics plus semantic ancestors up to ``dependency_depth`` hops."""
    if dependency_depth < 0:
        raise ValueError("dependency_depth must be >= 0")
    graph = catalog.graph()
    included: set[str] = set()
    for focal in focal_metric_ids:
        if focal not in catalog:
            raise KeyError(f"Unknown focal metric: {focal!r}")
        included.add(focal)
        frontier = {focal}
        for _ in range(dependency_depth):
            nxt: set[str] = set()
            for node in frontier:
                if node not in graph:
                    continue
                nxt.update(graph.predecessors(node))
            included.update(nxt)
            frontier = nxt
            if not frontier:
                break
    return included


def build_state_snapshot(
    catalog: MetricCatalog,
    runtime_state: Mapping[str, RuntimeNodeState],
    focal_metric_ids: Sequence[str],
    *,
    dependency_depth: int = 3,
    extra_metric_ids: Sequence[str] = (),
) -> MetricStateGraphSnapshot:
    """Slice the catalog around focals and attach runtime node facts.

    This is the candidate future integration point:

        OPEN transition → dependency graph snapshot → optional judgment → policy
    """
    focals = list(focal_metric_ids)
    if not focals:
        raise ValueError("focal_metric_ids must not be empty")
    included = nodes_within_dependency_depth(catalog, focals, dependency_depth=dependency_depth)
    included.update(extra_metric_ids)
    graph = catalog.graph()
    nodes = [
        _node_snapshot(catalog.get(metric_id), runtime_state.get(metric_id))
        for metric_id in included
    ]
    edges: list[DependencyEdge] = []
    for metric_id in included:
        if metric_id not in graph:
            continue
        for depends_on in graph.predecessors(metric_id):
            if depends_on in included:
                edges.append(DependencyEdge(metric_id=metric_id, depends_on=depends_on))
    return MetricStateGraphSnapshot(
        focal_metric_ids=focals,
        metrics=nodes,
        dependencies=edges,
    )


def snapshot_from_engine_statuses(
    catalog: MetricCatalog,
    statuses: Mapping[str, KPIStatus],
    focal_metric_ids: Sequence[str],
    *,
    dependency_depth: int = 3,
    observations: Mapping[str, KPIObservation] | None = None,
    records: Mapping[str, MetricStateRecord] | None = None,
) -> MetricStateGraphSnapshot:
    """Convenience wrapper: KPIStatus map → snapshot."""
    return build_state_snapshot(
        catalog,
        runtime_state_from_statuses(statuses, observations=observations, records=records),
        focal_metric_ids,
        dependency_depth=dependency_depth,
    )


def _anon_id(index: int) -> str:
    if index < 26:
        return f"metric_{chr(ord('a') + index)}"
    return f"metric_{index:02d}"


def anonymize_snapshot(
    snapshot: MetricStateGraphSnapshot,
) -> tuple[MetricStateGraphSnapshot, dict[str, str]]:
    """Replace metric ids/names with metric_a, metric_b, … Topology is preserved."""
    original_ids = sorted(item.metric_id for item in snapshot.metrics)
    mapping = {metric_id: _anon_id(index) for index, metric_id in enumerate(original_ids)}
    nodes = [
        item.model_copy(
            update={
                "metric_id": mapping[item.metric_id],
                "name": mapping[item.metric_id],
            }
        )
        for item in snapshot.metrics
    ]
    edges = [
        DependencyEdge(
            metric_id=mapping[edge.metric_id],
            depends_on=mapping[edge.depends_on],
        )
        for edge in snapshot.dependencies
    ]
    anonymized = MetricStateGraphSnapshot(
        focal_metric_ids=[mapping[metric_id] for metric_id in snapshot.focal_metric_ids],
        metrics=nodes,
        dependencies=edges,
    )
    return anonymized, mapping


def invert_mapping(mapping: Mapping[str, str]) -> dict[str, str]:
    return {anon: original for original, anon in mapping.items()}


def remap_target(target: str | None, mapping: Mapping[str, str] | None) -> str | None:
    """Map an anonymized target id back to the original metric id."""
    if target is None or mapping is None:
        return target
    inverse = invert_mapping(mapping)
    return inverse.get(target, mapping.get(target, target))


def shuffled_wire_json(snapshot: MetricStateGraphSnapshot, *, seed: int) -> str:
    """Non-canonical JSON with shuffled metric/edge arrays.

    Canonical ``sha256()`` is unchanged. This payload is only used for
    serialization-order robustness, not for the fair Jev/OpenAI comparison.
    """
    rng = random.Random(seed)
    payload = snapshot.model_dump(mode="json")
    _shuffle_snapshot_payload(payload, rng)
    return _unsorted_json(payload)


def shuffled_context_json(payload: dict[str, Any], *, seed: int) -> str:
    """Shuffle snapshot arrays inside a JudgmentContext provider payload."""
    rng = random.Random(seed)
    snap = dict(payload.get("snapshot") or {})
    _shuffle_snapshot_payload(snap, rng)
    shuffled = dict(payload)
    shuffled["snapshot"] = snap
    return _unsorted_json(shuffled)


def _shuffle_snapshot_payload(payload: dict[str, Any], rng: random.Random) -> None:
    metrics = list(payload.get("metrics") or [])
    edges = list(payload.get("dependencies") or [])
    focals = list(payload.get("focal_metric_ids") or [])
    rng.shuffle(metrics)
    rng.shuffle(edges)
    rng.shuffle(focals)
    payload["metrics"] = metrics
    payload["dependencies"] = edges
    payload["focal_metric_ids"] = focals


def shuffled_payload_hash(wire_json: str) -> str:
    return sha256_payload(json_loads(wire_json))


def _unsorted_json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"), default=str)


def json_loads(text: str) -> Any:
    import json

    return json.loads(text)


def add_irrelevant_nodes(
    snapshot: MetricStateGraphSnapshot,
    *,
    extras: Sequence[MetricNodeSnapshot] | None = None,
) -> MetricStateGraphSnapshot:
    """Attach NORMAL metrics with no path to the focals."""
    noise = list(extras) if extras is not None else _default_noise_nodes(snapshot)
    existing = {item.metric_id for item in snapshot.metrics}
    added = [item for item in noise if item.metric_id not in existing]
    return MetricStateGraphSnapshot(
        focal_metric_ids=list(snapshot.focal_metric_ids),
        metrics=[*snapshot.metrics, *added],
        dependencies=list(snapshot.dependencies),
    )


def _default_noise_nodes(snapshot: MetricStateGraphSnapshot) -> list[MetricNodeSnapshot]:
    used = {item.metric_id for item in snapshot.metrics}
    candidates = (
        ("headcount", "Headcount", "count", 120.0),
        ("website_sessions", "Website Sessions", "count", 8400.0),
        ("office_cost", "Office Cost", "EUR", 18500.0),
    )
    nodes: list[MetricNodeSnapshot] = []
    for metric_id, name, unit, value in candidates:
        if metric_id in used:
            continue
        nodes.append(
            MetricNodeSnapshot(
                metric_id=metric_id,
                name=name,
                unit=unit,
                current_value=value,
                baseline_value=value,
                previous_value=value,
                absolute_change=0.0,
                relative_change=0.0,
                state="NORMAL",
                quality="value",
            )
        )
    return nodes


def catalog_graph_for_snapshot(snapshot: MetricStateGraphSnapshot):
    """Rebuild the NetworkX semantic graph from snapshot edges (dep → metric)."""
    metrics = [
        Metric(
            id=item.metric_id,
            name=item.name or item.metric_id,
            formula=Formula.sum(item.metric_id),
            unit=item.unit or "unit",
            dependencies=tuple(
                edge.depends_on
                for edge in snapshot.dependencies
                if edge.metric_id == item.metric_id
            ),
        )
        for item in snapshot.metrics
    ]
    catalog = MetricCatalog(metrics)
    return build_business_graph(catalog.as_dict())


def active_metric_ids(snapshot: MetricStateGraphSnapshot) -> list[str]:
    return [
        item.metric_id
        for item in snapshot.metrics
        if item.state in ACTIVE_STATES and item.quality != NO_DATA_QUALITY
    ]


def candidate_target_ids(snapshot: MetricStateGraphSnapshot) -> list[str]:
    """Investigation-target options: active metrics in the slice, else focals."""
    active = active_metric_ids(snapshot)
    if active:
        return sorted(active)
    return list(snapshot.focal_metric_ids)
