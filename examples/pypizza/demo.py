"""PyPizza demo helpers on top of metric-runtime."""

from __future__ import annotations

from datetime import datetime

from metric_runtime.graph import build_business_graph, talk_subgraph
from metric_runtime.incidents import open_smart_incident
from metric_runtime.investigation import (
    investigate_window as _investigate_window,
)
from metric_runtime.investigation import (
    preferred_explanatory_path as _preferred_explanatory_path,
)
from metric_runtime.models import KPI, Incident, InvestigationResult

try:
    from .catalog import TALK_GRAPH_METRICS
except ImportError:  # pragma: no cover - marimo local path import
    from catalog import TALK_GRAPH_METRICS

PYPIZZA_PREFERRED_LEAVES = (
    "basket_threshold_concentration",
    "discount_cost_per_order",
)


def build_talk_graph(catalog: dict[str, KPI]):
    return talk_subgraph(build_business_graph(catalog), TALK_GRAPH_METRICS)


def preferred_explanatory_path(result: InvestigationResult) -> list[str]:
    return _preferred_explanatory_path(result, preferred_leaves=PYPIZZA_PREFERRED_LEAVES)


def investigate_window(engine, metric_name, start, end, filters=None):
    return _investigate_window(
        engine,
        metric_name,
        start,
        end,
        filters,
        preferred_leaves=PYPIZZA_PREFERRED_LEAVES,
    )


def open_pypizza_incident(
    engine,
    *,
    center_kpi: str = "weekend_profit",
    scope: dict[str, str] | None = None,
    start: datetime,
    windows: int = 12,
    interval_minutes: int = 30,
    persistence: int = 2,
    min_impact_eur: float = 50.0,
    quality=None,
) -> Incident | None:
    scope = dict(scope or {})
    context = ["Great Lunch campaign"] if scope.get("city") == "Amsterdam" else []
    return open_smart_incident(
        engine,
        center_kpi=center_kpi,
        scope=scope,
        start=start,
        windows=windows,
        interval_minutes=interval_minutes,
        persistence=persistence,
        min_impact_eur=min_impact_eur,
        quality=quality,
        preferred_leaves=PYPIZZA_PREFERRED_LEAVES,
        context=context,
    )
