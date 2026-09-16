"""Business semantic dependency graph.

This is a BUSINESS semantic graph, not technical lineage.
Graph visualization / presentation layout belongs in the example layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

import networkx as nx

from metric_runtime.models import Directionality, KPIStatus, Metric

if TYPE_CHECKING:
    from metric_runtime.engine import KPIEngine


def build_business_graph(catalog: Mapping[str, Metric]) -> nx.DiGraph:
    g = nx.DiGraph()
    for metric_id, metric in catalog.items():
        g.add_node(
            metric_id,
            name=metric.name,
            label=metric.display_name,
            owner=metric.owner,
            description=metric.description,
            unit=metric.unit.id,
            directionality=metric.directionality.value,
        )
    for metric_id, metric in catalog.items():
        for dep in metric.dependencies:
            if dep in catalog:
                g.add_edge(dep, metric_id)
    return g


def evaluate_graph_state(
    engine: KPIEngine,
    graph: nx.DiGraph,
    at: datetime,
    filters: dict[str, str] | None = None,
) -> dict[str, KPIStatus]:
    return {
        node: engine.evaluate(node, at, filters) for node in graph.nodes if node in engine.catalog
    }


def evaluate_graph_state_window(
    engine: KPIEngine,
    graph: nx.DiGraph,
    start: datetime,
    end: datetime,
    filters: dict[str, str] | None = None,
) -> dict[str, KPIStatus]:
    return {
        node: engine.evaluate_window(node, start, end, filters)
        for node in graph.nodes
        if node in engine.catalog
    }


def mark_root_candidates(
    catalog: Mapping[str, Metric],
    statuses: dict[str, KPIStatus],
) -> dict[str, KPIStatus]:
    updated: dict[str, KPIStatus] = {}
    for metric_id, status in statuses.items():
        is_root = False
        if status.anomaly:
            directional = catalog[metric_id].directionality in (
                Directionality.LOWER_IS_BAD,
                Directionality.HIGHER_IS_BAD,
            )
            blocking = []
            for d in catalog[metric_id].dependencies:
                if d not in statuses or not statuses[d].anomaly:
                    continue
                if catalog[d].directionality == Directionality.TWO_SIDED:
                    continue
                blocking.append(d)
            is_root = directional and not blocking
        updated[metric_id] = status.model_copy(update={"root_candidate": is_root})
    return updated


def find_explanatory_paths(
    catalog: Mapping[str, Metric],
    statuses: dict[str, KPIStatus],
    start: str,
) -> list[list[str]]:
    paths: list[list[str]] = []

    def walk(name: str, path: list[str]) -> None:
        status = statuses.get(name)
        if status is None or not status.anomaly:
            return
        deps = [
            d
            for d in catalog[name].dependencies
            if d in statuses and statuses[d].anomaly and d not in path
        ]
        deps.sort(
            key=lambda d: (
                0 if catalog[d].directionality.value != "two_sided" else 1,
                -abs(statuses[d].relative_change),
                -abs(statuses[d].z_score),
            )
        )
        if not deps:
            paths.append(path)
            return
        for dep in deps:
            walk(dep, path + [dep])

    if start in statuses and statuses[start].anomaly:
        walk(start, [start])
    return paths


def ancestors(graph: nx.DiGraph, metric: str) -> set[str]:
    if metric not in graph:
        return set()
    return set(nx.ancestors(graph, metric))


def descendants(graph: nx.DiGraph, metric: str) -> set[str]:
    if metric not in graph:
        return set()
    return set(nx.descendants(graph, metric))
