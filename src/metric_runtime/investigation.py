"""Graph-aware investigation.

Do NOT claim deterministic graph traversal proves causality.
Prefer: root candidate, explanatory metric, deepest anomalous dependency.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import networkx as nx

from metric_runtime.graph import (
    build_business_graph,
    find_explanatory_paths,
    mark_root_candidates,
)
from metric_runtime.models import ExplanatoryCandidate, InvestigationResult

if TYPE_CHECKING:
    from metric_runtime.engine import KPIEngine


def _depth_from_start(
    catalog: dict,
    start: str,
    name: str,
) -> int:
    """Shortest dependency-walk depth from start into deps."""
    if name == start:
        return 0
    best = 99
    stack = [(start, 0)]
    seen = {start}
    while stack:
        node, d = stack.pop()
        for dep in catalog[node].dependencies:
            if dep in seen:
                continue
            seen.add(dep)
            if dep == name:
                best = min(best, d + 1)
            stack.append((dep, d + 1))
    return best if best < 99 else 0


def rank_explanatory_candidates(
    engine: KPIEngine,
    statuses: dict,
    start: str,
    filters: dict[str, str] | None = None,
    at: datetime | None = None,
    *,
    preferred_leaves: tuple[str, ...] = (),
    deprioritize_sides: tuple[str, ...] = ("operations",),
) -> list[ExplanatoryCandidate]:
    catalog = engine.catalog_dict
    candidates: list[ExplanatoryCandidate] = []
    for name, status in statuses.items():
        if not status.root_candidate:
            continue
        depth = _depth_from_start(catalog, start, name)
        impact = 0.0
        if at is not None:
            impact = engine.estimate_impact_eur(
                name, at, status.value, status.baseline_mean, filters
            )
        leaf_rank = 0 if name in preferred_leaves else 1
        if catalog[name].graph_side in deprioritize_sides:
            leaf_rank = 3
        score = float(depth) * 10.0 - leaf_rank * 100.0 + min(status.severity, 50.0)
        candidates.append(
            ExplanatoryCandidate(
                name=name,
                owner=catalog[name].owner,
                depth=depth,
                status=status,
                impact_eur=impact,
                score=score,
            )
        )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def investigate_metric(
    engine: KPIEngine,
    metric_name: str,
    at: datetime,
    filters: dict[str, str] | None = None,
    *,
    preferred_leaves: tuple[str, ...] = (),
) -> InvestigationResult:
    catalog = engine.catalog_dict
    graph = build_business_graph(catalog)

    if metric_name not in graph:
        nodes = {metric_name}
    else:
        nodes = set(nx.ancestors(graph, metric_name)) | {metric_name}

    statuses = {n: engine.evaluate(n, at, filters) for n in nodes if n in catalog}
    statuses = mark_root_candidates(catalog, statuses)
    anomalous = [s for s in statuses.values() if s.anomaly]
    roots = [s for s in statuses.values() if s.root_candidate]
    paths = find_explanatory_paths(catalog, statuses, start=metric_name)
    normal_deps = [
        d for d in catalog[metric_name].dependencies if d in statuses and not statuses[d].anomaly
    ]
    deepest = rank_explanatory_candidates(
        engine,
        statuses,
        metric_name,
        filters,
        at,
        preferred_leaves=preferred_leaves,
    )
    start_status = statuses.get(metric_name)
    impact = 0.0
    if start_status is not None:
        impact = engine.estimate_impact_eur(
            metric_name,
            at,
            start_status.value,
            start_status.baseline_mean,
            filters,
        )
    return InvestigationResult(
        metric=metric_name,
        anomalous_metrics=anomalous,
        normal_dependencies=normal_deps,
        root_candidates=roots,
        explanatory_paths=paths,
        deepest_candidates=deepest,
        primary_explanatory=deepest[0] if deepest else None,
        impact_eur=impact,
    )


def investigate_window(
    engine: KPIEngine,
    metric_name: str,
    start: datetime,
    end: datetime,
    filters: dict[str, str] | None = None,
    *,
    preferred_leaves: tuple[str, ...] = (),
) -> InvestigationResult:
    catalog = engine.catalog_dict
    graph = build_business_graph(catalog)

    if metric_name not in graph:
        nodes = {metric_name}
    else:
        nodes = set(nx.ancestors(graph, metric_name)) | {metric_name}

    statuses = {n: engine.evaluate_window(n, start, end, filters) for n in nodes if n in catalog}
    statuses = mark_root_candidates(catalog, statuses)
    anomalous = [s for s in statuses.values() if s.anomaly]
    roots = [s for s in statuses.values() if s.root_candidate]
    paths = find_explanatory_paths(catalog, statuses, start=metric_name)
    normal_deps = [
        d for d in catalog[metric_name].dependencies if d in statuses and not statuses[d].anomaly
    ]
    deepest = rank_explanatory_candidates(
        engine,
        statuses,
        metric_name,
        filters,
        end,
        preferred_leaves=preferred_leaves,
    )
    start_status = statuses[metric_name]
    impact = engine.estimate_impact_eur(
        metric_name,
        end,
        start_status.value,
        start_status.baseline_mean,
        filters,
    )
    return InvestigationResult(
        metric=metric_name,
        anomalous_metrics=anomalous,
        normal_dependencies=normal_deps,
        root_candidates=roots,
        explanatory_paths=paths,
        deepest_candidates=deepest,
        primary_explanatory=deepest[0] if deepest else None,
        impact_eur=impact,
    )


def preferred_explanatory_path(
    result: InvestigationResult,
    preferred_leaves: tuple[str, ...] = (),
) -> list[str]:
    """Pick the path that ends at the primary deepest candidate when possible."""
    primary = result.primary_explanatory.name if result.primary_explanatory else None
    if primary:
        for path in result.explanatory_paths:
            if path and path[-1] == primary:
                return path
    for leaf in preferred_leaves:
        for path in result.explanatory_paths:
            if path and path[-1] == leaf:
                return path
    if result.explanatory_paths:
        return max(result.explanatory_paths, key=len)
    return [result.metric]
