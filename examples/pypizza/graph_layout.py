from __future__ import annotations

import math
from collections import defaultdict

import networkx as nx


def dependency_depths(
    graph: nx.DiGraph,
    center_metric: str,
    max_depth: int | None = None,
) -> dict[str, int]:
    """Shortest dependency depth walking into predecessors from center."""
    depths: dict[str, int] = {center_metric: 0}
    queue = [center_metric]
    while queue:
        node = queue.pop(0)
        if max_depth is not None and depths[node] >= max_depth:
            continue
        for pred in graph.predecessors(node):
            if pred not in depths:
                depths[pred] = depths[node] + 1
                queue.append(pred)
    return depths


def _ring_for_node(graph: nx.DiGraph, node: str, depth: int) -> int:
    """Prefer explicit catalog ring when present; else BFS depth."""
    ring = graph.nodes[node].get("graph_ring")
    if isinstance(ring, int) and ring >= 0:
        return ring
    return depth


def _primary_parent(
    graph: nx.DiGraph,
    node: str,
    depths: dict[str, int],
) -> str | None:
    """One parent toward the center so each node is placed once."""
    parents = [s for s in graph.successors(node) if s in depths and depths[s] < depths[node]]
    if not parents:
        return None
    parents.sort(key=lambda n: (depths[n], n))
    return parents[0]


def _side_of(graph: nx.DiGraph, node: str) -> str:
    side = graph.nodes[node].get("graph_side")
    if side in {"marketing", "finance", "operations", "center"}:
        return side
    return "finance"


# Hand-tuned talk layout. balances regions and leaves room for full labels.
PRESENTATION_POSITIONS: dict[str, tuple[float, float]] = {
    "weekend_profit": (0.0, 0.15),
    # Marketing (left)
    "revenue": (-2.35, 0.95),
    "orders": (-4.35, 0.95),
    "new_customers": (-5.85, 2.55),
    "opportunities": (-6.15, 0.55),
    "marketing_leads": (-5.85, -0.85),
    # Finance (right)
    "profit_margin": (2.35, 0.95),
    "average_cost_per_order": (4.25, 2.15),
    "delivery_cost_per_order": (5.85, 3.25),
    "discount_cost_per_order": (6.15, 1.35),
    "average_order_value": (4.25, -0.55),
    # Basket mix. one node per band, fanned out under average order size
    "basket_share_under_20": (6.55, -0.15),
    "basket_threshold_concentration": (7.35, -1.45),
    "basket_share_25_29": (7.35, -2.85),
    "basket_share_30_39": (6.45, -4.05),
    "basket_share_40_plus": (4.95, -4.75),
    # Operations (bottom)
    "average_delivery_time": (0.0, -2.55),
    "late_delivery_rate": (0.0, -4.35),
}


def presentation_dependency_layout(
    graph: nx.DiGraph,
    center_metric: str,
    *,
    max_depth: int | None = 3,
) -> dict[str, tuple[float, float]]:
    """
    Presentation layout: prefer fixed talk coordinates, fall back to radial.
    """
    depths = dependency_depths(graph, center_metric, max_depth=max_depth)
    visible = {
        n
        for n, d in depths.items()
        if max_depth is None or _ring_for_node(graph, n, d) <= max_depth
    }
    pos = {n: PRESENTATION_POSITIONS[n] for n in visible if n in PRESENTATION_POSITIONS}
    missing = [n for n in visible if n not in pos]
    if missing:
        radial = radial_dependency_layout(
            graph, center_metric, max_depth=max_depth, radius_step=1.7
        )
        for n in missing:
            if n in radial:
                pos[n] = radial[n]
    return pos


def radial_dependency_layout(
    graph: nx.DiGraph,
    center_metric: str,
    *,
    max_depth: int | None = 3,
    radius_step: float = 1.7,
) -> dict[str, tuple[float, float]]:
    """
    Three-sector radial layout (fallback).

    Marketing left · Finance right · Operations bottom.
    """
    if center_metric not in graph:
        raise ValueError(f"{center_metric} not in graph")

    depths = dependency_depths(graph, center_metric, max_depth=max_depth)
    rings: dict[str, int] = {n: _ring_for_node(graph, n, d) for n, d in depths.items()}
    if max_depth is not None:
        rings = {n: r for n, r in rings.items() if r <= max_depth}

    children: dict[str, list[str]] = defaultdict(list)
    for node in rings:
        if node == center_metric:
            continue
        parent = _primary_parent(graph, node, depths)
        if parent is None or parent not in rings:
            parent = center_metric
        children[parent].append(node)

    preferred = {
        "revenue": 0,
        "orders": 0,
        "new_customers": 0,
        "opportunities": 1,
        "marketing_leads": 2,
        "profit_margin": 0,
        "average_order_value": 0,
        "average_cost_per_order": 1,
        "basket_threshold_concentration": 0,
        "discount_cost_per_order": 0,
        "delivery_cost_per_order": 1,
        "average_delivery_time": 0,
        "late_delivery_rate": 0,
    }
    side_order = {"marketing": 0, "finance": 1, "operations": 2, "center": 3}
    for parent, kids in children.items():
        kids.sort(
            key=lambda n: (
                side_order.get(_side_of(graph, n), 9),
                rings[n],
                preferred.get(n, 50),
                n,
            )
        )

    def subtree_weight(node: str) -> int:
        return 1 + sum(subtree_weight(c) for c in children.get(node, []))

    pos: dict[str, tuple[float, float]] = {center_metric: (0.0, 0.0)}

    def place(node: str, angle_start: float, angle_end: float) -> None:
        kids = children.get(node, [])
        if not kids:
            return
        same_ring = len({rings[k] for k in kids}) == 1
        weights = [1] * len(kids) if same_ring else [subtree_weight(k) for k in kids]
        total_w = sum(weights) or 1
        span = angle_end - angle_start
        pad = span * 0.06 if len(kids) > 1 else 0.0
        usable = span - pad * len(kids)
        cursor = angle_start + pad / 2
        for kid, w in zip(kids, weights):
            kid_span = usable * (w / total_w)
            mid = cursor + kid_span / 2.0
            r = radius_step * max(rings[kid], 1)
            pos[kid] = (r * math.cos(mid), r * math.sin(mid))
            inset = kid_span * 0.10
            place(kid, cursor + inset, cursor + kid_span - inset)
            cursor += kid_span + pad

    mkt = [n for n in children.get(center_metric, []) if _side_of(graph, n) == "marketing"]
    fin = [n for n in children.get(center_metric, []) if _side_of(graph, n) == "finance"]
    ops = [n for n in children.get(center_metric, []) if _side_of(graph, n) == "operations"]
    other = [
        n for n in children.get(center_metric, []) if n not in mkt and n not in fin and n not in ops
    ]

    children[center_metric] = []
    if mkt:
        children[center_metric] = list(mkt)
        place(center_metric, math.pi * 0.62, math.pi * 1.38)
    if fin:
        children[center_metric] = list(fin)
        place(center_metric, -math.pi * 0.38, math.pi * 0.38)
    if ops:
        children[center_metric] = list(ops)
        place(center_metric, -math.pi * 0.92, -math.pi * 0.58)
    if other:
        children[center_metric] = list(other)
        place(center_metric, -math.pi / 2, -math.pi / 2 + 2 * math.pi)
    children[center_metric] = mkt + fin + ops + other
    return pos


def talk_subgraph(
    full: nx.DiGraph,
    metric_names: tuple[str, ...],
) -> nx.DiGraph:
    g = nx.DiGraph()
    keep = set(metric_names)
    for n in keep:
        if n in full:
            g.add_node(n, **full.nodes[n])
    for u, v in full.edges:
        if u in keep and v in keep:
            g.add_edge(u, v)
    return g
