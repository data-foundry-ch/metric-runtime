"""Deterministic GraphAnalysis over a MetricStateGraphSnapshot.

An active dependency branch is a DIRECT dependency of a focal metric whose
subtree contains at least one active metric (OPEN / DETECTED / ACKNOWLEDGED,
quality != no_data).

Example::

    gross_margin OPEN
    ├── revenue NORMAL
    └── cost_of_goods OPEN
        ├── unit_cost OPEN
        └── volume NORMAL

active_branch_count = 1

cost_of_goods and unit_cost are both active, but they belong to the same
direct-dependency branch.

This module contains only graph facts. It does not emit operational judgments.
"""

from __future__ import annotations

from collections import defaultdict

from examples.metric_judgment_eval.models import (
    ActivePath,
    GraphAnalysis,
    InvestigationCandidate,
    MetricNodeSnapshot,
    MetricStateGraphSnapshot,
)
from examples.metric_judgment_eval.snapshot import ACTIVE_STATES, NO_DATA_QUALITY

# Formula objects in current cases are dummy ``sum(self)`` shapes and are not
# present on MetricStateGraphSnapshot. Polarity of ratio/difference formulas
# is therefore not recoverable from provider input.
DIRECTIONAL_SUPPORT_KNOWN = False


def is_active(node: MetricNodeSnapshot | None) -> bool:
    if node is None:
        return False
    return node.state in ACTIVE_STATES and node.quality != NO_DATA_QUALITY


def is_missing(node: MetricNodeSnapshot | None) -> bool:
    if node is None:
        return True
    return node.quality == NO_DATA_QUALITY or node.state == "NO_DATA"


def children_map(snapshot: MetricStateGraphSnapshot) -> dict[str, list[str]]:
    """metric_id → direct dependency ids (semantic children toward leaves)."""
    out: dict[str, list[str]] = defaultdict(list)
    for edge in snapshot.dependencies:
        out[edge.metric_id].append(edge.depends_on)
    return {key: sorted(dict.fromkeys(values)) for key, values in out.items()}


def _has_active_in_subtree(
    metric_id: str,
    nodes: dict[str, MetricNodeSnapshot],
    children: dict[str, list[str]],
    seen: set[str] | None = None,
) -> bool:
    seen = set() if seen is None else seen
    if metric_id in seen:
        return False
    seen.add(metric_id)
    node = nodes.get(metric_id)
    if is_active(node):
        return True
    return any(
        _has_active_in_subtree(child, nodes, children, seen)
        for child in children.get(metric_id, [])
    )


def active_branches(snapshot: MetricStateGraphSnapshot, focal_id: str) -> list[str]:
    """Direct dependencies of ``focal_id`` whose subtree contains an active metric."""
    nodes = snapshot.metric_map()
    children = children_map(snapshot)
    return [
        dep for dep in children.get(focal_id, []) if _has_active_in_subtree(dep, nodes, children)
    ]


def active_paths(snapshot: MetricStateGraphSnapshot) -> list[tuple[str, ...]]:
    """Every path from each focal to an active descendant, including intermediates."""
    nodes = snapshot.metric_map()
    children = children_map(snapshot)
    paths: list[tuple[str, ...]] = []

    def walk(metric_id: str, path: tuple[str, ...], seen: set[str]) -> None:
        if metric_id in seen:
            return
        nxt_seen = seen | {metric_id}
        for child in children.get(metric_id, []):
            child_path = path + (child,)
            if is_active(nodes.get(child)):
                paths.append(child_path)
            if _has_active_in_subtree(child, nodes, children, set(nxt_seen)):
                walk(child, child_path, nxt_seen)

    for focal in snapshot.focal_metric_ids:
        walk(focal, (focal,), set())
    unique = sorted(set(paths), key=lambda item: (len(item), item))
    return unique


def terminal_active_descendants(
    snapshot: MetricStateGraphSnapshot, start: str
) -> list[tuple[str, int]]:
    """Active descendants of ``start`` with no deeper active dependency, plus depth."""
    nodes = snapshot.metric_map()
    children = children_map(snapshot)
    found: list[tuple[str, int]] = []

    def walk(metric_id: str, depth: int, seen: set[str]) -> None:
        if metric_id in seen:
            return
        seen = seen | {metric_id}
        active_kids = [
            child for child in children.get(metric_id, []) if is_active(nodes.get(child))
        ]
        inactive_but_deeper = [
            child
            for child in children.get(metric_id, [])
            if child not in active_kids and _has_active_in_subtree(child, nodes, children)
        ]
        if is_active(nodes.get(metric_id)) and metric_id != start and not active_kids:
            found.append((metric_id, depth))
        for child in active_kids:
            walk(child, depth + 1, seen)
        for child in inactive_but_deeper:
            walk(child, depth + 1, seen)

    walk(start, 0, set())
    # Prefer strictly terminal: drop a node if it appears as an ancestor of another found node.
    ids = {metric_id for metric_id, _depth in found}
    children_all = children
    descendants: dict[str, set[str]] = {}

    def closure(metric_id: str) -> set[str]:
        if metric_id in descendants:
            return descendants[metric_id]
        acc: set[str] = set()
        stack = list(children_all.get(metric_id, []))
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            acc.add(node)
            stack.extend(children_all.get(node, []))
        descendants[metric_id] = acc
        return acc

    terminals = [(metric_id, depth) for metric_id, depth in found if not (ids & closure(metric_id))]
    return sorted(dict.fromkeys(terminals), key=lambda item: (-item[1], item[0]))


def shared_active_dependencies(snapshot: MetricStateGraphSnapshot) -> list[str]:
    """Active metrics that lie on dependency paths of at least two focals."""
    children = children_map(snapshot)
    nodes = snapshot.metric_map()
    focals = snapshot.focal_metric_ids
    if len(focals) < 2:
        return _diamond_shared(snapshot, focals[0], children, nodes) if focals else []

    def descendants_of(metric_id: str) -> set[str]:
        found: set[str] = set()
        stack = list(children.get(metric_id, []))
        seen = {metric_id}
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            found.add(node)
            stack.extend(children.get(node, []))
        return found

    coverage: dict[str, int] = defaultdict(int)
    for focal in focals:
        for dep in descendants_of(focal):
            coverage[dep] += 1
    shared = [
        metric_id
        for metric_id, count in coverage.items()
        if count >= 2 and is_active(nodes.get(metric_id))
    ]
    return sorted(shared)


def _diamond_shared(
    snapshot: MetricStateGraphSnapshot,
    focal: str,
    children: dict[str, list[str]],
    nodes: dict[str, MetricNodeSnapshot],
) -> list[str]:
    branches = children.get(focal, [])
    if len(branches) < 2:
        return []

    def closure(start: str) -> set[str]:
        found = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            for child in children.get(node, []):
                if child not in found:
                    found.add(child)
                    stack.append(child)
        return found

    shared: set[str] | None = None
    for branch in branches:
        nodes_in_branch = closure(branch)
        shared = nodes_in_branch if shared is None else shared & nodes_in_branch
    if not shared:
        return []
    return sorted(metric_id for metric_id in shared if is_active(nodes.get(metric_id)))


def missing_evidence_ids(snapshot: MetricStateGraphSnapshot) -> list[str]:
    """Direct dependencies of focals whose observation is missing or NO_DATA."""
    nodes = snapshot.metric_map()
    children = children_map(snapshot)
    found: list[str] = []
    for focal in snapshot.focal_metric_ids:
        if is_missing(nodes.get(focal)):
            found.append(focal)
        for dep in children.get(focal, []):
            if is_missing(nodes.get(dep)):
                found.append(dep)
    return sorted(dict.fromkeys(found))


def no_data_ids(snapshot: MetricStateGraphSnapshot) -> list[str]:
    return sorted(
        node.metric_id
        for node in snapshot.metrics
        if node.quality == NO_DATA_QUALITY or node.state == "NO_DATA"
    )


def _candidate(
    snapshot: MetricStateGraphSnapshot, metric_id: str, depth: int
) -> InvestigationCandidate:
    node = snapshot.metric_map()[metric_id]
    children = children_map(snapshot)
    terminal = not any(
        is_active(snapshot.metric_map().get(child)) for child in children.get(metric_id, [])
    )
    return InvestigationCandidate(
        metric_id=metric_id,
        depth=depth,
        state=node.state,
        relative_change=node.relative_change,
        absolute_change=node.absolute_change,
        terminal_active=terminal,
    )


def analyze_graph(snapshot: MetricStateGraphSnapshot) -> GraphAnalysis:
    """Compute structural facts. Never infers cause or operational sufficiency."""
    nodes = snapshot.metric_map()
    children = children_map(snapshot)
    focals = tuple(snapshot.focal_metric_ids)
    direct_deps = sorted({dep for focal in focals for dep in children.get(focal, [])})
    branch_roots = sorted({dep for focal in focals for dep in active_branches(snapshot, focal)})
    paths = tuple(ActivePath(metric_ids=path) for path in active_paths(snapshot))

    per_focal_terminals = {focal: terminal_active_descendants(snapshot, focal) for focal in focals}
    competing = any(len(items) > 1 for items in per_focal_terminals.values())

    if len(focals) == 1:
        ranked = per_focal_terminals.get(focals[0], [])
    elif shared_active_dependencies(snapshot):
        ranked = []
        for focal in focals:
            ranked.extend(per_focal_terminals.get(focal, []))
        ranked = sorted(dict.fromkeys(ranked), key=lambda item: (-item[1], item[0]))
    else:
        ranked = []

    if ranked:
        max_depth = max(depth for _metric_id, depth in ranked)
        deepest_ids = tuple(sorted(metric_id for metric_id, depth in ranked if depth == max_depth))
        if len(focals) == 1 and not competing:
            candidate_ids = deepest_ids
        elif competing:
            candidate_ids = tuple(sorted(metric_id for metric_id, _depth in ranked))
        else:
            candidate_ids = deepest_ids
    else:
        max_depth = 0
        deepest_ids = ()
        candidate_ids = ()

    depth_by_id = {metric_id: depth for metric_id, depth in ranked}
    candidates = tuple(
        _candidate(snapshot, metric_id, depth_by_id.get(metric_id, 0))
        for metric_id in candidate_ids
    )

    shared = tuple(shared_active_dependencies(snapshot))
    missing = tuple(missing_evidence_ids(snapshot))
    no_data = tuple(no_data_ids(snapshot))
    focal_has_active = any(
        _has_active_in_subtree(dep, nodes, children)
        for focal in focals
        for dep in children.get(focal, [])
    )

    return GraphAnalysis(
        focal_metric_ids=focals,
        active_direct_dependency_count=len(direct_deps),
        active_branch_count=len(branch_roots),
        active_paths=paths,
        max_active_depth=max_depth,
        deepest_active_metric_ids=deepest_ids,
        investigation_candidates=candidates,
        shared_active_dependency_ids=shared,
        missing_evidence_metric_ids=missing,
        no_data_metric_ids=no_data,
        competing_active_branches=competing,
        focal_has_active_dependency=focal_has_active,
        multiple_focals_share_active_dependency=len(focals) >= 2 and bool(shared),
        directionally_supporting_metric_ids=(),
        directionally_conflicting_metric_ids=(),
        directional_support_known=DIRECTIONAL_SUPPORT_KNOWN,
    )
