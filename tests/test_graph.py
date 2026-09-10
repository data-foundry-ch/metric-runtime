"""Graph helpers."""

from __future__ import annotations

import networkx as nx

from metric_runtime import KPI, KPICatalog
from metric_runtime.graph import build_business_graph, mark_root_candidates
from metric_runtime.models import Directionality, KPIState, KPIStatus


def _status(name: str, *, anomaly: bool, directionality: Directionality) -> KPIStatus:
    return KPIStatus(
        name=name,
        value=1.0,
        baseline_mean=1.0,
        baseline_std=0.1,
        z_score=5.0 if anomaly else 0.0,
        relative_change=0.5 if anomaly else 0.0,
        anomaly=anomaly,
        support=100,
        support_ok=True,
        as_of="t",
        directionality=directionality,
        state=KPIState.DETECTED if anomaly else KPIState.NORMAL,
        severity=5.0 if anomaly else 0.0,
    )


def test_graph_acyclic_and_edges():
    cat = KPICatalog(
        [
            KPI(name="leaf", owner="a"),
            KPI(name="mid", owner="b", dependencies=("leaf",)),
            KPI(name="top", owner="c", dependencies=("mid",)),
        ]
    )
    g = build_business_graph(cat.as_dict())
    assert nx.is_directed_acyclic_graph(g)
    assert g.has_edge("leaf", "mid")
    assert g.has_edge("mid", "top")


def test_root_candidates_ignore_two_sided():
    catalog = {
        "basket_threshold_concentration": KPI(
            name="basket_threshold_concentration",
            owner="promo",
            directionality=Directionality.HIGHER_IS_BAD,
        ),
        "orders": KPI(
            name="orders",
            owner="growth",
            directionality=Directionality.TWO_SIDED,
        ),
    }
    statuses = {
        "basket_threshold_concentration": _status(
            "basket_threshold_concentration",
            anomaly=True,
            directionality=Directionality.HIGHER_IS_BAD,
        ),
        "orders": _status("orders", anomaly=True, directionality=Directionality.TWO_SIDED),
    }
    marked = mark_root_candidates(catalog, statuses)
    assert marked["basket_threshold_concentration"].root_candidate is True
