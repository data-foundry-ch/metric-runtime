"""Pure logic tests for the Jev vs LLM judgment comparison example.

CI must never contact external model providers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from examples.judgment_comparison.evidence import (
    build_evidence,
    canonical_json,
    canonical_json_bytes,
    evidence_hash,
    packet_for_models,
)
from examples.judgment_comparison.judgments import (
    BusinessDriver,
    BusinessJudgment,
    FixtureJudgmentProvider,
    JudgmentExperiment,
    JudgmentRun,
    RecommendedAction,
    _usage_dict,
    compare_costs,
    compare_judgments,
    cost_stats,
    detect_availability,
    estimate_jev_cost_usd,
    experiment_consistency,
    extract_cost_usd,
    field_distribution,
    fixture_run,
    format_cost_usd,
    latency_stats,
    redact_secrets,
    run_experiment,
)
from examples.judgment_comparison.policy import (
    ActionRoute,
    apply_comparison_policy,
    apply_policy,
)
from examples.judgment_comparison.scenarios import (
    DISPLAY_METRIC_IDS,
    FOCAL_METRIC_ID,
    SCENARIOS,
    build_catalog,
    catalog_graph,
    get_scenario,
    list_scenarios,
    run_scenario_runtime,
    series_for_scenario,
)
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "examples" / "judgment_comparison" / "app.py"


def _judgment(**kwargs) -> BusinessJudgment:
    defaults = dict(
        material=True,
        promotion_related=True,
        primary_driver=BusinessDriver.promotion_behavior,
        needs_human_review=True,
        action=RecommendedAction.review_promotion,
    )
    defaults.update(kwargs)
    return BusinessJudgment.model_validate(defaults)


def test_four_scenarios_exist():
    ids = [item.id for item in list_scenarios()]
    assert ids == ["great_lunch", "healthy_growth", "cost_pressure", "ambiguous"]
    assert set(SCENARIOS) == set(ids)


def test_unknown_scenario_errors():
    with pytest.raises(KeyError, match="Unknown scenario"):
        get_scenario("not-a-scenario")


def test_catalog_uses_current_metric_api():
    catalog = build_catalog()
    catalog.validate()
    assert FOCAL_METRIC_ID in catalog
    assert set(DISPLAY_METRIC_IDS) <= set(catalog.ids())
    graph = catalog_graph()
    assert graph.has_edge("revenue", "profit_margin")
    assert graph.has_edge("basket_threshold_concentration", "average_order_value")
    assert graph.has_edge("average_cost_per_order", "profit_margin")


def test_great_lunch_runtime_shape():
    snapshot = run_scenario_runtime(get_scenario("great_lunch"))
    revenue = snapshot.statuses["revenue"]
    orders = snapshot.statuses["orders"]
    margin = snapshot.statuses["profit_margin"]
    aov = snapshot.statuses["average_order_value"]
    cost = snapshot.statuses["average_cost_per_order"]
    threshold = snapshot.statuses["basket_threshold_concentration"]
    assert revenue.relative_change == pytest.approx(0.14, abs=0.015)
    assert orders.relative_change == pytest.approx(0.21, abs=0.015)
    assert margin.value - margin.baseline_mean == pytest.approx(-6.6, abs=0.15)
    assert aov.relative_change == pytest.approx(-0.17, abs=0.015)
    assert cost.relative_change == pytest.approx(0.12, abs=0.015)
    assert threshold.relative_change == pytest.approx(0.44, abs=0.02)
    assert margin.anomaly
    assert threshold.anomaly
    assert snapshot.process_result.transition.current.value == "OPEN"
    assert snapshot.explanatory_path == [
        "profit_margin",
        "revenue",
        "average_order_value",
        "basket_threshold_concentration",
    ]


def test_healthy_growth_is_not_concerning():
    snapshot = run_scenario_runtime(get_scenario("healthy_growth"))
    margin = snapshot.statuses["profit_margin"]
    assert not margin.anomaly
    assert snapshot.statuses["revenue"].relative_change > 0.05
    assert snapshot.statuses["orders"].relative_change > 0.05
    assert snapshot.process_result.transition.current.value in {"NORMAL", "DETECTED"}


def test_cost_pressure_runtime_shape():
    snapshot = run_scenario_runtime(get_scenario("cost_pressure"))
    assert snapshot.statuses["average_cost_per_order"].anomaly
    assert snapshot.statuses["profit_margin"].anomaly
    assert not snapshot.statuses["basket_threshold_concentration"].anomaly
    assert abs(snapshot.statuses["average_order_value"].relative_change) < 0.08


def test_ambiguous_has_facts_but_mild_drivers():
    snapshot = run_scenario_runtime(get_scenario("ambiguous"))
    margin = snapshot.statuses["profit_margin"]
    assert margin.value < margin.baseline_mean
    assert abs(snapshot.statuses["average_order_value"].relative_change) < 0.08
    assert abs(snapshot.statuses["average_cost_per_order"].relative_change) < 0.08
    assert snapshot.statuses["basket_threshold_concentration"].relative_change < 0.12
    evidence = build_evidence(snapshot)
    assert evidence.context.campaign_name == "Great Lunch"
    assert evidence.runtime_state in {"OPEN", "DETECTED", "NORMAL"}


def test_evidence_packet_is_canonical_and_hashed():
    snapshot = run_scenario_runtime(get_scenario("great_lunch"))
    evidence = build_evidence(snapshot)
    packet, text, digest = packet_for_models(evidence)
    again = canonical_json(packet)
    assert text == again
    assert digest == hashlib.sha256(text.encode("utf-8")).hexdigest()
    parsed = json.loads(text)
    assert list(parsed.keys()) == ["context", "incident", "investigation", "metrics"]
    assert parsed["incident"]["metric"] == "profit_margin"
    assert "revenue" in parsed["metrics"]
    assert parsed["metrics"]["revenue"]["change_pct"] == pytest.approx(14.0, abs=1.5)
    assert parsed["metrics"]["profit_margin"]["change_percentage_points"] == pytest.approx(
        -6.6, abs=0.2
    )
    # Byte-stable: sorted keys, no whitespace.
    assert canonical_json_bytes(packet) == text.encode("utf-8")
    assert evidence_hash(packet) == digest


def test_both_fixture_providers_see_the_same_packet():
    snapshot = run_scenario_runtime(get_scenario("great_lunch"))
    evidence = build_evidence(snapshot)
    _, text, digest = packet_for_models(evidence)
    jev = FixtureJudgmentProvider("jev", "great_lunch").judge(text, evidence_hash=digest)
    llm = FixtureJudgmentProvider("llm", "great_lunch").judge(text, evidence_hash=digest)
    assert jev.evidence_hash == llm.evidence_hash == digest
    assert jev.simulated and llm.simulated
    assert jev.judgment is not None and llm.judgment is not None
    assert jev.judgment.primary_driver != llm.judgment.primary_driver
    assert jev.judgment.action == llm.judgment.action


def test_business_judgment_rejects_unknown_driver():
    with pytest.raises(ValidationError):
        BusinessJudgment(
            material=True,
            promotion_related=True,
            primary_driver=" vibes",  # type: ignore[arg-type]
            needs_human_review=True,
            action=RecommendedAction.observe,
        )


def test_agreement_counts_field_matches():
    left = _judgment()
    right = _judgment(primary_driver=BusinessDriver.product_mix)
    agreement = compare_judgments(left, right)
    assert agreement.agreements == 4
    assert agreement.total == 5
    assert agreement.material is True
    assert agreement.primary_driver is False
    assert "4 / 5" in agreement.summary


def test_policy_is_conservative():
    observe = apply_policy(
        _judgment(material=False, needs_human_review=False, action=RecommendedAction.observe)
    )
    assert observe.route == ActionRoute.monitor
    assert observe.autonomous is False

    review = apply_policy(_judgment(needs_human_review=True))
    assert review.route == ActionRoute.human_review

    promo = apply_policy(
        _judgment(needs_human_review=False, action=RecommendedAction.review_promotion)
    )
    assert promo.route == ActionRoute.promotion_review
    assert promo.autonomous is False

    costs = apply_policy(
        _judgment(
            needs_human_review=False,
            promotion_related=False,
            primary_driver=BusinessDriver.cost_pressure,
            action=RecommendedAction.investigate_costs,
        )
    )
    assert costs.route == ActionRoute.cost_investigation


def test_jev_low_confidence_routes_to_review():
    judgment = _judgment(needs_human_review=False, action=RecommendedAction.review_promotion)
    run = JudgmentRun(
        provider="jev",
        model="typesafe:jev-latest",
        judgment=judgment,
        provider_details={"confidence": {"primary_driver": 0.42, "action": 0.88}},
        evidence_hash="abc",
    )
    decision = apply_policy(judgment, confidence_metadata=run)
    assert decision.route == ActionRoute.human_review
    assert decision.reason_code == "low_jev_confidence"


def test_llm_does_not_get_fabricated_confidence_gate():
    judgment = _judgment(needs_human_review=False, action=RecommendedAction.review_promotion)
    run = JudgmentRun(
        provider="llm",
        model="openai:gpt-4o",
        judgment=judgment,
        evidence_hash="abc",
    )
    decision = apply_policy(judgment, confidence_metadata=run)
    assert decision.route == ActionRoute.promotion_review


def test_comparison_policy_disagreement_is_human_review():
    jev = JudgmentRun(
        provider="jev",
        model="fixture:jev",
        judgment=_judgment(),
        evidence_hash="abc",
    )
    llm = JudgmentRun(
        provider="llm",
        model="fixture:llm",
        judgment=_judgment(primary_driver=BusinessDriver.product_mix),
        evidence_hash="abc",
    )
    decision = apply_comparison_policy(jev, llm)
    assert decision.route == ActionRoute.human_review
    assert decision.reason_code == "model_disagreement"
    assert "primary_driver" in decision.detail


def test_fixture_providers_cover_all_scenarios():
    for scenario_id in SCENARIOS:
        jev = fixture_run("jev", scenario_id, "hash")
        llm = fixture_run("llm", scenario_id, "hash")
        assert jev.judgment is not None
        assert llm.judgment is not None
        assert jev.simulated and llm.simulated


def test_ambiguous_fixtures_prefer_human_review():
    jev = fixture_run("jev", "ambiguous", "hash")
    llm = fixture_run("llm", "ambiguous", "hash")
    assert jev.judgment is not None and llm.judgment is not None
    assert jev.judgment.needs_human_review and llm.judgment.needs_human_review
    assert jev.judgment.action == RecommendedAction.human_review
    agreement = compare_judgments(jev.judgment, llm.judgment)
    assert agreement.primary_driver is False


def test_repeated_run_consistency_is_not_accuracy():
    def fake_runner(provider, text, digest):
        del text
        driver = (
            BusinessDriver.promotion_behavior
            if provider.name == "jev"
            else (
                BusinessDriver.promotion_behavior
                if fake_runner.counter[provider.name] < 3
                else BusinessDriver.product_mix
            )
        )
        fake_runner.counter[provider.name] += 1
        return JudgmentRun(
            provider=provider.name,
            model=provider.model,
            judgment=_judgment(primary_driver=driver),
            latency_ms=10.0,
            evidence_hash=digest,
        )

    fake_runner.counter = {"jev": 0, "llm": 0}  # type: ignore[attr-defined]
    jev = FixtureJudgmentProvider("jev", "great_lunch")
    llm = FixtureJudgmentProvider("llm", "great_lunch")
    experiment = run_experiment(
        jev=jev,
        llm=llm,
        evidence_text="{}",
        evidence_hash="abc",
        n=5,
        runner=fake_runner,
    )
    assert isinstance(experiment, JudgmentExperiment)
    jev_dist = experiment_consistency(experiment, "jev")
    llm_dist = experiment_consistency(experiment, "llm")
    assert jev_dist["primary_driver"].consistency == 1.0
    assert llm_dist["primary_driver"].consistency == pytest.approx(0.6)
    assert jev_dist["primary_driver"].mode_count == 5
    stats = latency_stats(experiment.runs)
    assert stats is not None
    assert stats["n"] == 10
    with pytest.raises(ValueError, match="capped at 5"):
        run_experiment(jev=jev, llm=llm, evidence_text="{}", evidence_hash="abc", n=6)


def test_field_distribution_booleans():
    runs = [
        JudgmentRun(
            provider="llm",
            model="x",
            judgment=_judgment(material=True),
            evidence_hash="h",
        )
        for _ in range(4)
    ] + [
        JudgmentRun(
            provider="llm",
            model="x",
            judgment=_judgment(
                material=False, needs_human_review=False, action=RecommendedAction.observe
            ),
            evidence_hash="h",
        )
    ]
    dist = field_distribution(runs, "material")
    assert dist.mode == "True"
    assert dist.consistency == pytest.approx(0.8)


def test_redact_secrets(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_secret_value")
    assert "[redacted]" in redact_secrets("failed ts_secret_value")
    assert "ts_secret_value" not in redact_secrets("failed ts_secret_value")


def test_availability_does_not_require_keys():
    availability = detect_availability()
    assert availability.jev_model
    assert availability.llm_key in {True, False}


def test_series_keys_are_weekly():
    scenario = get_scenario("great_lunch")
    series = series_for_scenario(scenario)
    assert set(series) == set(DISPLAY_METRIC_IDS)
    assert len(series["profit_margin"]) >= 7


def test_app_is_marimo_and_gates_live_calls():
    text = APP.read_text(encoding="utf-8")
    assert "marimo.App" in text
    assert "Run comparison" in text
    assert "Fixture" in text
    assert "Ask AI" not in text
    assert "chat bubble" not in text.lower()


def test_sync_provider_call_works_inside_running_event_loop():
    import asyncio

    from examples.judgment_comparison.judgments import _call_sync

    async def inside_loop() -> str:
        return _call_sync(lambda: "ok")

    assert asyncio.run(inside_loop()) == "ok"


def test_no_explanation_field_on_shared_contract():
    schema = BusinessJudgment.model_json_schema()
    assert "explanation" not in schema["properties"]
    assert set(schema["properties"]) == {
        "material",
        "promotion_related",
        "primary_driver",
        "needs_human_review",
        "action",
    }


def test_usage_dict_extracts_decimal_cost():
    from decimal import Decimal

    class _Usage:
        input_tokens = 120
        output_tokens = 18
        cost = Decimal("0.004200")

    class _Result:
        usage = _Usage()

    dumped = _usage_dict(_Result())
    assert dumped is not None
    assert dumped["input_tokens"] == 120
    assert dumped["cost"] == pytest.approx(0.0042)


def test_usage_dict_omits_unknown_cost():
    class _Usage:
        input_tokens = 10
        cost = None

    class _Result:
        usage = _Usage()

    dumped = _usage_dict(_Result())
    assert dumped is not None
    assert "cost" not in dumped


def test_extract_cost_prefers_usage_over_provider_details():
    assert extract_cost_usd(usage={"cost": 0.01}, provider_details={"cost": 9.9}) == pytest.approx(
        0.01
    )
    assert extract_cost_usd(usage=None, provider_details={"cost": "0.0025"}) == pytest.approx(
        0.0025
    )
    assert extract_cost_usd(usage={"cost": 0.5}, simulated=True) is None
    assert extract_cost_usd(usage={"cost": 0}) == 0.0
    assert extract_cost_usd(usage={}, provider_details={}) is None
    assert extract_cost_usd(usage={"cost": float("nan")}) is None


def test_fixture_runs_have_no_monetary_cost():
    run = fixture_run("jev", "great_lunch", "hash")
    assert run.simulated
    assert run.cost_usd is None
    assert run.usage is None
    comparison = compare_costs(run, fixture_run("llm", "great_lunch", "hash"))
    assert comparison.jev.state == "simulated"
    assert comparison.llm.state == "simulated"
    assert comparison.delta_usd is None
    assert comparison.jev.display == "n/a (simulated)"


def test_cost_comparison_reports_delta_only_when_both_priced():
    jev = JudgmentRun(
        provider="jev",
        model="typesafe:jev-latest",
        judgment=_judgment(),
        cost_usd=0.0012,
        evidence_hash="h",
    )
    llm = JudgmentRun(
        provider="llm",
        model="openai:gpt-4o",
        judgment=_judgment(primary_driver=BusinessDriver.product_mix),
        cost_usd=0.018,
        evidence_hash="h",
    )
    both = compare_costs(jev, llm)
    assert both.comparable
    assert both.delta_usd == pytest.approx(0.0168)
    missing = compare_costs(jev, llm.model_copy(update={"cost_usd": None}))
    assert missing.delta_usd is None
    assert missing.llm.state == "not_reported"
    assert missing.llm.display == "not reported"


def test_simulated_cost_is_ignored_even_if_a_number_is_present():
    run = fixture_run("jev", "great_lunch", "hash").model_copy(update={"cost_usd": 0.99})
    comparison = compare_costs(run, None)
    assert comparison.jev.state == "simulated"
    assert comparison.jev.cost_usd is None
    assert comparison.delta_usd is None


def test_cost_stats_omit_unreported_and_simulated():
    priced = [
        JudgmentRun(
            provider="llm",
            model="x",
            judgment=_judgment(),
            cost_usd=0.02,
            evidence_hash="h",
        ),
        JudgmentRun(
            provider="llm",
            model="x",
            judgment=_judgment(),
            cost_usd=0.04,
            evidence_hash="h",
        ),
        JudgmentRun(
            provider="llm",
            model="x",
            judgment=_judgment(),
            evidence_hash="h",
        ),
        JudgmentRun(
            provider="llm",
            model="fixture:llm",
            judgment=_judgment(),
            cost_usd=9.0,
            simulated=True,
            evidence_hash="h",
        ),
    ]
    stats = cost_stats(priced)
    assert stats is not None
    assert stats["n"] == 2
    assert stats["n_runs"] == 4
    assert stats["total"] == pytest.approx(0.06)
    assert stats["median"] == pytest.approx(0.03)
    assert cost_stats([priced[2]]) is None


def test_format_cost_usd_sign_and_sub_cent():
    assert format_cost_usd(None) == "not reported"
    assert format_cost_usd(0) == "$0"
    assert format_cost_usd(0.018) == "$0.0180"
    assert format_cost_usd(0.000042).startswith("$0.000042")
    assert format_cost_usd(-0.0012).startswith("-$")


def test_jev_cost_is_estimated_from_input_tokens():
    from examples.judgment_comparison.judgments import JEV_COST_ESTIMATE_NOTE, assign_cost
    from examples.judgment_comparison.viz import spend_time_html

    assert estimate_jev_cost_usd({"input_tokens": 1_000_000}) == pytest.approx(0.042)
    assert estimate_jev_cost_usd({"input_tokens": 1000}) == pytest.approx(0.000042)
    assert estimate_jev_cost_usd({"request_tokens": 500}) == pytest.approx(0.000021)
    assert estimate_jev_cost_usd({}) is None
    assert estimate_jev_cost_usd(None) is None

    amount, source = assign_cost(provider="jev", usage={"input_tokens": 2000})
    assert source == "estimated"
    assert amount == pytest.approx(2000 * 0.042 / 1_000_000)

    billed, billed_source = assign_cost(provider="jev", usage={"input_tokens": 5000, "cost": 0.01})
    assert billed_source == "usage"
    assert billed == pytest.approx(0.01)

    llm_amount, llm_source = assign_cost(provider="llm", usage={"input_tokens": 2000})
    assert llm_amount is None
    assert llm_source is None

    jev = JudgmentRun(
        provider="jev",
        model="typesafe:jev-latest",
        judgment=_judgment(),
        usage={"input_tokens": 1000},
        evidence_hash="h",
    )
    llm = JudgmentRun(
        provider="llm",
        model="openai:gpt-4o",
        judgment=_judgment(primary_driver=BusinessDriver.product_mix),
        cost_usd=0.01,
        evidence_hash="h",
    )
    comparison = compare_costs(jev, llm)
    assert comparison.jev.state == "estimated"
    assert comparison.jev.note == JEV_COST_ESTIMATE_NOTE
    assert comparison.jev.cost_usd == pytest.approx(0.000042)
    assert comparison.comparable
    assert JEV_COST_ESTIMATE_NOTE in spend_time_html(jev, llm)


def test_judgment_details_use_plain_language():
    from examples.judgment_comparison.viz import judgment_details_html

    jev = fixture_run("jev", "great_lunch", "h")
    llm = fixture_run("llm", "great_lunch", "h")
    html = judgment_details_html(jev, llm)
    assert "Should we pay attention?" in html
    assert "Is the promotion involved?" in html
    assert "What's driving this?" in html
    assert "Does a person need to review?" in html
    assert "What should happen next?" in html
    assert "Promotion behaviour" in html
    assert "Product mix" in html
    assert "Review the promotion" in html
    assert "Agree" in html
    assert "Differ" in html


def test_app_includes_cost_comparison_panel():
    text = APP.read_text(encoding="utf-8")
    assert "spend_time_html" in text
    assert "judgment_details_html" in text
    assert "architecture_html" not in text
    assert "Ask AI" not in text
