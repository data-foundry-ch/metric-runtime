"""PyPizza flagship example integration tests."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import duckdb
import networkx as nx
import pytest

from metric_runtime.detectors import SeasonalZScoreDetector, ThresholdDetector
from metric_runtime.engine import KPIEngine
from metric_runtime.graph import build_business_graph, mark_root_candidates
from metric_runtime.models import (
    DetectorConfig,
    Directionality,
    IncidentState,
    KPIState,
    QualityReport,
)
from metric_runtime.state import StatePolicy, evolve_state

ROOT = Path(__file__).resolve().parents[1]
PYPIZZA = ROOT / "examples" / "pypizza"
DB = PYPIZZA / "data" / "pypizza.duckdb"
sys.path.insert(0, str(PYPIZZA))

from catalog import TALK_GRAPH_METRICS, build_catalog  # noqa: E402
from demo import (  # noqa: E402
    investigate_window,
    open_pypizza_incident,
    preferred_explanatory_path,
)
from graph_layout import presentation_dependency_layout, radial_dependency_layout  # noqa: E402
from impact import campaign_impact_decomposition  # noqa: E402
from quality import check_data_quality  # noqa: E402
from queries import basket_distribution  # noqa: E402

START = datetime(2026, 5, 15, 11, 30)
END = datetime(2026, 5, 17, 13, 30)
PRE_START = datetime(2026, 5, 8, 11, 30)
PRE_END = datetime(2026, 5, 10, 13, 30)
AMS_LUNCH = {"city": "Amsterdam", "meal_period": "lunch"}


@pytest.fixture(scope="module")
def engine():
    if not DB.exists():
        pytest.skip("pypizza.duckdb missing. run examples/pypizza/generate_data.py")
    con = duckdb.connect(str(DB), read_only=True)
    yield KPIEngine(build_catalog(), connection=con, fact_table="pypizza_halfhourly")
    con.close()


def test_catalog_is_acyclic():
    g = build_business_graph(build_catalog())
    assert nx.is_directed_acyclic_graph(g)


def test_semantic_edges():
    g = build_business_graph(build_catalog())
    assert g.has_edge("profit_margin", "weekend_profit")
    assert g.has_edge("basket_threshold_concentration", "average_order_value")
    talk = {n for n in TALK_GRAPH_METRICS if n in g}
    for n in talk:
        if n == "weekend_profit":
            continue
        parents = [s for s in g.successors(n) if s in talk]
        assert len(parents) == 1, f"{n} parents={parents}"


def test_hemispheres_split_marketing_finance():
    g = build_business_graph(build_catalog())
    pos = presentation_dependency_layout(g, "weekend_profit", max_depth=3)
    assert pos["revenue"][0] < 0
    assert pos["profit_margin"][0] > 0
    assert pos["average_delivery_time"][1] < 0


def test_radial_layout_deterministic():
    g = build_business_graph(build_catalog())
    a = radial_dependency_layout(g, "weekend_profit", max_depth=3)
    b = radial_dependency_layout(g, "weekend_profit", max_depth=3)
    assert a == b
    assert a["weekend_profit"] == (0.0, 0.0)


def test_pre_campaign_healthy(engine: KPIEngine):
    st = engine.evaluate_window("weekend_profit", PRE_START, PRE_END, AMS_LUNCH)
    assert st.value > 0
    assert not st.anomaly


def test_campaign_story_shape(engine: KPIEngine):
    imp = campaign_impact_decomposition(engine, START, END, AMS_LUNCH)
    assert imp["pct_change"]["orders"] > 0.10
    assert imp["pct_change"]["revenue"] > 0.02
    assert imp["pct_change"]["weekend_profit"] < 0.0
    assert imp["profit_margin_pct"] < -0.10
    assert imp["average_order_value_pct"] < -0.05
    assert imp["average_cost_per_order_pct"] > 0.02
    assert imp["threshold_concentration_pct"] > 0.40


def test_basket_cliff(engine: KPIEngine):
    dist = basket_distribution(engine, START, END, {**AMS_LUNCH, "campaign": "great_lunch"})
    by = {r["basket_band"]: r["orders"] for r in dist}
    total = sum(by.values()) or 1
    assert by.get("20-24.99", 0) / total > 0.40


def test_investigation_finds_threshold(engine: KPIEngine):
    inv = investigate_window(engine, "weekend_profit", START, END, AMS_LUNCH)
    assert any(s.name == "weekend_profit" and s.anomaly for s in inv.anomalous)
    path = preferred_explanatory_path(inv)
    assert "profit_margin" in path
    assert "average_order_value" in path or "average_cost_per_order" in path
    assert inv.primary_explanatory is not None
    assert inv.primary_explanatory.name in {
        "basket_threshold_concentration",
        "discount_cost_per_order",
    }
    names = {c.name for c in inv.deepest_candidates}
    assert "basket_threshold_concentration" in names


def test_smart_alert_owner_is_promotions(engine: KPIEngine):
    incident = open_pypizza_incident(
        engine,
        center_kpi="weekend_profit",
        scope=AMS_LUNCH,
        start=datetime(2026, 5, 15, 11, 30),
        windows=12,
        persistence=2,
        min_impact_eur=40,
    )
    assert incident is not None
    assert incident.state in {IncidentState.OPEN, IncidentState.DETECTED}
    assert "Commercial Growth" in incident.owner or "Promotions" in incident.owner
    assert incident.owner != "Finance" or incident.explanatory_kpi != "weekend_profit"


def test_quality_blocks_incident(engine: KPIEngine):
    bad = QualityReport(
        healthy=False,
        freshness_ok=False,
        completeness_ok=True,
        volume_ok=True,
        row_count=0,
        missing_rate=0.0,
        latest_ts="",
        message="stale",
    )
    assert (
        open_pypizza_incident(
            engine,
            scope=AMS_LUNCH,
            start=datetime(2026, 5, 15, 11, 30),
            windows=8,
            quality=bad,
        )
        is None
    )


def test_detector_swap_without_catalog_change(engine: KPIEngine):
    st = engine.evaluate_window("profit_margin", START, END, AMS_LUNCH)
    thr = ThresholdDetector().evaluate(
        "profit_margin",
        st.value,
        [st.baseline_mean] * 6,
        directionality=Directionality.LOWER_IS_BAD,
        config=DetectorConfig(
            min_relative_change=0.05,
            absolute_threshold=st.baseline_mean * 0.95,
        ),
        support=st.support,
        support_ok=True,
        as_of=st.as_of,
    )
    assert isinstance(engine.detector, SeasonalZScoreDetector)
    assert thr.anomaly is True


def test_state_requires_persistence(engine: KPIEngine):
    st = engine.evaluate("weekend_profit", datetime(2026, 5, 15, 12, 0), AMS_LUNCH)
    one = evolve_state(
        [st],
        policy=StatePolicy(persistence=2, min_impact_eur=1),
        impact_eur=100,
    )
    two = evolve_state(
        [st, st],
        policy=StatePolicy(persistence=2, min_impact_eur=1),
        impact_eur=100,
    )
    if st.anomaly and st.support_ok:
        assert one == KPIState.DETECTED
        assert two == KPIState.OPEN


def test_data_quality_healthy(engine: KPIEngine):
    q = check_data_quality(engine, datetime(2026, 5, 17, 12, 0))
    assert q.healthy


def test_pypizza_config_loader(engine: KPIEngine):
    from metric_runtime.config.factory import build_runtime

    built = build_runtime(
        "local",
        project_config=PYPIZZA / "metric-runtime.yaml",
        connections_config=PYPIZZA / "connections.yaml",
    )
    assert len(built.catalog) == len(engine.catalog)


def test_talk_graph_rings():
    cat = build_catalog()
    assert cat["weekend_profit"].graph_ring == 0
    assert cat["profit_margin"].graph_ring == 1
    assert cat["basket_threshold_concentration"].graph_ring == 3


def test_mark_root_candidates_smoke():
    catalog = build_catalog()
    statuses = {
        "basket_threshold_concentration": __import__(
            "metric_runtime.models", fromlist=["KPIStatus"]
        ).KPIStatus(
            name="basket_threshold_concentration",
            value=0.5,
            baseline_mean=0.18,
            baseline_std=0.02,
            z_score=16,
            relative_change=1.7,
            anomaly=True,
            support=100,
            support_ok=True,
            as_of="t",
            directionality=Directionality.HIGHER_IS_BAD,
            state=KPIState.DETECTED,
            severity=20,
        )
    }
    marked = mark_root_candidates(catalog, statuses)
    assert marked["basket_threshold_concentration"].root_candidate is True
