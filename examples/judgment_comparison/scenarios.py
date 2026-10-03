"""Deterministic PyPizza economics scenarios evaluated by Metric Runtime.

Each scenario supplies a weekly time series. Metric Runtime calculates current
values, seasonal baselines, detections, operational state, and investigation
paths. Judgment models never see the raw series — only the evidence packet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from metric_runtime.calculations import BatchCalculation, BatchRegistry, CallableBatchSource
from metric_runtime.catalog import MetricCatalog
from metric_runtime.detectors import SeasonalZScore
from metric_runtime.engine import KPIEngine
from metric_runtime.graph import build_business_graph
from metric_runtime.investigation import investigate_metric, preferred_explanatory_path
from metric_runtime.models import (
    Directionality,
    InvestigationResult,
    KPIStatus,
    Metric,
    ProcessResult,
)
from metric_runtime.state import StatePolicy
from metric_runtime.stores import InMemoryStateStore

SOURCE_NAME = "scenario_metrics"
FOCAL_METRIC_ID = "profit_margin"
EVAL_AT = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
LOOKBACK_WEEKS = 6
HISTORY_WEEKS = 8

DISPLAY_METRIC_IDS = (
    "revenue",
    "orders",
    "profit_margin",
    "average_order_value",
    "average_cost_per_order",
    "basket_threshold_concentration",
)

# Top-down explanatory layout (not force-directed). y decreases down the tree.
GRAPH_LAYOUT: dict[str, tuple[float, float]] = {
    "profit_margin": (0.0, 3.2),
    "revenue": (-1.7, 1.7),
    "average_cost_per_order": (1.7, 1.7),
    "average_order_value": (-2.6, 0.2),
    "orders": (-0.6, 0.2),
    "basket_threshold_concentration": (-2.6, -1.4),
}


class BusinessContext(BaseModel):
    scenario: str
    scenario_id: str
    campaign_name: str | None = None
    campaign_rule: str | None = None
    city: str = "Amsterdam"
    meal_period: str = "lunch"


class Scenario(BaseModel):
    """Demo scenario: design expectation is not experimentally verified truth."""

    id: str
    name: str
    summary: str
    context: BusinessContext
    baselines: dict[str, float]
    current: dict[str, float]
    expected_driver: str | None = None
    expected_action: str | None = None
    notes: str = ""


class HistoryPoint(BaseModel):
    at: datetime
    value: float
    is_current: bool = False
    anomalous: bool = False


class RuntimeSnapshot(BaseModel):
    scenario: Scenario
    evaluated_at: datetime
    statuses: dict[str, KPIStatus]
    process_result: ProcessResult
    investigation: InvestigationResult
    history: list[HistoryPoint]
    explanatory_path: list[str]
    metric_names: dict[str, str]
    metric_units: dict[str, str]


def _batch(metric_id: str) -> BatchCalculation:
    return BatchCalculation(source=SOURCE_NAME, result=metric_id)


def build_catalog() -> MetricCatalog:
    """Focused Great Lunch economics graph (subset of the PyPizza story)."""
    sharp = SeasonalZScore(lookback_periods=LOOKBACK_WEEKS, threshold=2.0, min_relative_change=0.08)
    margin = SeasonalZScore(
        lookback_periods=LOOKBACK_WEEKS, threshold=2.0, min_relative_change=0.05
    )
    cliff = SeasonalZScore(lookback_periods=LOOKBACK_WEEKS, threshold=1.8, min_relative_change=0.12)

    return MetricCatalog(
        [
            Metric(
                id="profit_margin",
                name="Profit Margin",
                description="Contribution profit as a percentage of revenue.",
                owner="Finance",
                calculation=_batch("profit_margin"),
                dependencies=("revenue", "average_cost_per_order"),
                directionality=Directionality.LOWER_IS_BAD,
                detector=margin,
                unit="percent",
            ),
            Metric(
                id="revenue",
                name="Revenue",
                description="Gross order value before discounts.",
                owner="Commercial",
                calculation=_batch("revenue"),
                dependencies=("average_order_value", "orders"),
                directionality=Directionality.TWO_SIDED,
                detector=sharp,
                unit="eur",
            ),
            Metric(
                id="orders",
                name="Orders",
                description="Completed lunch-delivery orders.",
                owner="Growth",
                calculation=_batch("orders"),
                directionality=Directionality.TWO_SIDED,
                detector=sharp,
                unit="count",
            ),
            Metric(
                id="average_order_value",
                name="Average Order Value",
                description="Gross order value per completed order.",
                owner="Commercial Growth",
                calculation=_batch("average_order_value"),
                dependencies=("basket_threshold_concentration",),
                directionality=Directionality.LOWER_IS_BAD,
                detector=sharp,
                unit="eur",
            ),
            Metric(
                id="average_cost_per_order",
                name="Average Cost / Order",
                description="Platform cost per order (discount + delivery + payment).",
                owner="Commercial Operations",
                calculation=_batch("average_cost_per_order"),
                directionality=Directionality.HIGHER_IS_BAD,
                detector=sharp,
                unit="eur",
            ),
            Metric(
                id="basket_threshold_concentration",
                name="Basket Threshold Concentration",
                description="Share of orders with basket €20–€24.99, just above the €20 cliff.",
                owner="Commercial Growth / Promotions",
                calculation=_batch("basket_threshold_concentration"),
                directionality=Directionality.HIGHER_IS_BAD,
                detector=cliff,
                unit="percent",
            ),
        ],
        name="pypizza-judgment-demo",
    )


def _unit_variations() -> list[float]:
    raw = (0.992, 1.008, 0.997, 1.004, 0.999, 1.003)
    mean = sum(raw) / len(raw)
    return [value / mean for value in raw]


def series_for_scenario(scenario: Scenario) -> dict[str, dict[datetime, float]]:
    """Weekly comparable-lunch points: lookback weeks plus current."""
    variations = _unit_variations()
    series: dict[str, dict[datetime, float]] = {}
    extra = HISTORY_WEEKS - LOOKBACK_WEEKS
    for metric_id, baseline in scenario.baselines.items():
        points: dict[datetime, float] = {}
        for week in range(LOOKBACK_WEEKS, 0, -1):
            at = EVAL_AT - timedelta(weeks=week)
            points[at] = baseline * variations[LOOKBACK_WEEKS - week]
        for week in range(extra, 0, -1):
            at = EVAL_AT - timedelta(weeks=LOOKBACK_WEEKS + week)
            # Pre-lookback: same comparable level, slightly quieter.
            points[at] = baseline * (0.998 + 0.002 * ((week % 2) * 2 - 1))
        points[EVAL_AT] = scenario.current[metric_id]
        series[metric_id] = points
    return series


def _handler_for(series: dict[str, dict[datetime, float]]):
    def handler(context) -> dict[str, float | None]:
        at = context.effective_at
        row: dict[str, float | None] = {}
        for metric_id, points in series.items():
            value = points.get(at)
            if value is None:
                for stamp, item in points.items():
                    if stamp == at or stamp.isoformat() == at.isoformat():
                        value = item
                        break
            row[metric_id] = None if value is None else float(value)
        return row

    return handler


def build_engine(scenario: Scenario) -> KPIEngine:
    catalog = build_catalog()
    series = series_for_scenario(scenario)
    registry = BatchRegistry()
    registry.register(SOURCE_NAME, CallableBatchSource(SOURCE_NAME, _handler_for(series)))
    return KPIEngine(
        catalog,
        batch_registry=registry,
        state_store=InMemoryStateStore(),
        state_policy=StatePolicy(persistence=1, min_impact=0.0, require_support=False),
    )


def run_scenario_runtime(scenario: Scenario) -> RuntimeSnapshot:
    """Run Metric Runtime for one scenario. No judgment model is involved."""
    engine = build_engine(scenario)
    session = engine.new_evaluation_session()
    statuses = {
        metric_id: engine.evaluate(metric_id, EVAL_AT, session=session)
        for metric_id in DISPLAY_METRIC_IDS
    }
    process_result = engine.process(FOCAL_METRIC_ID, at=EVAL_AT, session=session)
    investigation = investigate_metric(engine, FOCAL_METRIC_ID, EVAL_AT)
    path = preferred_explanatory_path(
        investigation,
        preferred_leaves=("basket_threshold_concentration", "average_cost_per_order"),
    )
    series = series_for_scenario(scenario)
    focal_series = series[FOCAL_METRIC_ID]
    current_status = statuses[FOCAL_METRIC_ID]
    history = [
        HistoryPoint(
            at=stamp,
            value=value,
            is_current=stamp == EVAL_AT,
            anomalous=bool(stamp == EVAL_AT and current_status.anomaly),
        )
        for stamp, value in sorted(focal_series.items())
    ]
    catalog = engine.kpi_catalog
    return RuntimeSnapshot(
        scenario=scenario,
        evaluated_at=EVAL_AT,
        statuses=statuses,
        process_result=process_result,
        investigation=investigation,
        history=history,
        explanatory_path=path,
        metric_names={metric_id: catalog.get(metric_id).name for metric_id in DISPLAY_METRIC_IDS},
        metric_units={
            metric_id: catalog.get(metric_id).unit.id for metric_id in DISPLAY_METRIC_IDS
        },
    )


def catalog_graph():
    return build_business_graph(build_catalog().as_dict())


def _scenario(
    *,
    id: str,
    name: str,
    summary: str,
    campaign_name: str | None,
    campaign_rule: str | None,
    baselines: dict[str, float],
    current: dict[str, float],
    expected_driver: str | None,
    expected_action: str | None,
    notes: str,
) -> Scenario:
    return Scenario(
        id=id,
        name=name,
        summary=summary,
        context=BusinessContext(
            scenario=name,
            scenario_id=id,
            campaign_name=campaign_name,
            campaign_rule=campaign_rule,
        ),
        baselines=baselines,
        current=current,
        expected_driver=expected_driver,
        expected_action=expected_action,
        notes=notes,
    )


_BASE = {
    "revenue": 100_000.0,
    "orders": 4_000.0,
    "profit_margin": 18.0,
    "average_order_value": 25.0,
    "average_cost_per_order": 8.0,
    "basket_threshold_concentration": 18.0,
}


SCENARIOS: dict[str, Scenario] = {
    "great_lunch": _scenario(
        id="great_lunch",
        name="Great Lunch",
        summary=(
            "Amsterdam weekday lunch campaign: €10 off baskets ≥ €20. "
            "Orders and revenue rise; economics deteriorate."
        ),
        campaign_name="Great Lunch",
        campaign_rule="€10 off baskets >= €20",
        baselines=_BASE,
        current={
            "revenue": 114_000.0,
            "orders": 4_840.0,
            "profit_margin": 11.4,
            "average_order_value": 20.75,
            "average_cost_per_order": 8.96,
            "basket_threshold_concentration": 25.92,
        },
        expected_driver="promotion_behavior",
        expected_action="review_promotion",
        notes="Everything worked as designed. The business outcome was still undesirable.",
    ),
    "healthy_growth": _scenario(
        id="healthy_growth",
        name="Healthy Growth",
        summary="Volume is up and unit economics are stable or improving. No campaign distortion.",
        campaign_name=None,
        campaign_rule=None,
        baselines=_BASE,
        current={
            "revenue": 112_000.0,
            "orders": 4_400.0,
            "profit_margin": 18.4,
            "average_order_value": 25.25,
            "average_cost_per_order": 7.92,
            "basket_threshold_concentration": 18.2,
        },
        expected_driver="volume",
        expected_action="observe",
        notes="Growth without margin deterioration.",
    ),
    "cost_pressure": _scenario(
        id="cost_pressure",
        name="Cost Pressure",
        summary="Revenue and AOV are stable; cost per order rises and margin falls. No basket cliff.",
        campaign_name=None,
        campaign_rule=None,
        baselines=_BASE,
        current={
            "revenue": 102_000.0,
            "orders": 4_040.0,
            "profit_margin": 13.5,
            "average_order_value": 25.1,
            "average_cost_per_order": 9.44,
            "basket_threshold_concentration": 18.4,
        },
        expected_driver="cost_pressure",
        expected_action="investigate_costs",
        notes="Evidence is consistent with cost pressure rather than promotion behaviour.",
    ),
    "ambiguous": _scenario(
        id="ambiguous",
        name="Ambiguous",
        summary=(
            "Campaign is active. Margin is moderately down; AOV, cost, and threshold "
            "concentration only moved mildly. Facts are clear; interpretation is not."
        ),
        campaign_name="Great Lunch",
        campaign_rule="€10 off baskets >= €20",
        baselines=_BASE,
        current={
            "revenue": 105_000.0,
            "orders": 4_320.0,
            "profit_margin": 16.0,
            "average_order_value": 24.25,
            "average_cost_per_order": 8.32,
            "basket_threshold_concentration": 19.44,
        },
        expected_driver="unclear",
        expected_action="human_review",
        notes="Deterministic facts with less certain causal / business interpretation.",
    ),
}


def list_scenarios() -> list[Scenario]:
    return [
        SCENARIOS[key] for key in ("great_lunch", "healthy_growth", "cost_pressure", "ambiguous")
    ]


def get_scenario(scenario_id: str) -> Scenario:
    try:
        return SCENARIOS[scenario_id]
    except KeyError as exc:
        known = ", ".join(SCENARIOS)
        raise KeyError(f"Unknown scenario {scenario_id!r}. Known: {known}") from exc
