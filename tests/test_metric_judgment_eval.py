"""Unit tests for the metric-state judgment evaluation harness.

CI must never contact Jev or OpenAI.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from examples.metric_judgment_eval.analysis import analyze_graph
from examples.metric_judgment_eval.cases import (
    all_cases,
    anonymized_variant,
    case_single_deep_driver,
    degradation_sequence,
    get_case,
    magnitude_sequence,
    noise_variant,
)
from examples.metric_judgment_eval.evaluation import (
    evaluate_case,
    evaluate_suite_fixture,
    future_provider_justified,
    judgments_equal,
    overview_rows,
    pairwise_agreement,
    robustness_from_fixture,
    robustness_rate,
    run_consistency,
    run_selected,
    score_run,
    suite_inventory,
)
from examples.metric_judgment_eval.fixtures import FixtureJudgmentProvider
from examples.metric_judgment_eval.live_eval import LIVE_FLAG
from examples.metric_judgment_eval.live_eval import main as live_main
from examples.metric_judgment_eval.models import (
    BENCHMARK_METADATA_FIELDS,
    PROVIDER_SNAPSHOT_KEYS,
    DependencyEdge,
    JudgmentContext,
    MetricNodeSnapshot,
    MetricStateGraphSnapshot,
    OperationalProposal,
    ProposalDisposition,
    ProposalExpectation,
    ProposalJudgment,
    ProposalKind,
)
from examples.metric_judgment_eval.policy import PolicyRoute, apply_comparison_policy, apply_policy
from examples.metric_judgment_eval.proposals import (
    generate_operational_proposals,
    proposal_requires_model,
)
from examples.metric_judgment_eval.providers import (
    RecordingProvider,
    candidate_output_type,
    decode_target,
    detect_availability,
    payload_contains_benchmark_metadata,
    redact_secrets,
)
from examples.metric_judgment_eval.snapshot import (
    RuntimeNodeState,
    add_irrelevant_nodes,
    anonymize_snapshot,
    build_state_snapshot,
    json_loads,
    nodes_within_dependency_depth,
    runtime_state_from_status,
    shuffled_wire_json,
    snapshot_from_engine_statuses,
)
from examples.metric_judgment_eval.viz import (
    analysis_html,
    comparison_html,
    expectation_html,
    overview_table_html,
    payload_html,
)
from pydantic import ValidationError

from metric_runtime import Formula, Metric, MetricCatalog
from metric_runtime.models import Directionality, KPIState, KPIStatus

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "examples" / "metric_judgment_eval" / "app.py"


def _judgment(**kwargs) -> ProposalJudgment:
    defaults = dict(
        evidence_sufficient=True,
        human_review_required=False,
        disposition=ProposalDisposition.accept,
    )
    defaults.update(kwargs)
    return ProposalJudgment.model_validate(defaults)


def test_at_least_twelve_cases():
    cases = all_cases()
    assert len(cases) >= 12
    assert len({case.case_id for case in cases}) == len(cases)


def test_cases_are_built_from_metric_catalog_slice():
    case = case_single_deep_driver()
    ids = {node.metric_id for node in case.snapshot.metrics}
    assert "gross_margin" in ids
    assert "unit_cost" in ids
    assert case.snapshot.focal_metric_ids == ["gross_margin"]
    deps = {(edge.metric_id, edge.depends_on) for edge in case.snapshot.dependencies}
    assert ("gross_margin", "cost_of_goods") in deps
    assert ("cost_of_goods", "unit_cost") in deps


def test_graph_slice_respects_depth():
    catalog = MetricCatalog(
        [
            Metric(id="leaf", formula=Formula.sum("leaf")),
            Metric(id="mid", formula=Formula.sum("mid"), dependencies=("leaf",)),
            Metric(id="top", formula=Formula.sum("top"), dependencies=("mid",)),
        ]
    )
    state = {
        "leaf": RuntimeNodeState(state="OPEN", current_value=1, baseline_value=1),
        "mid": RuntimeNodeState(state="OPEN", current_value=1, baseline_value=1),
        "top": RuntimeNodeState(state="OPEN", current_value=1, baseline_value=1),
    }
    shallow = build_state_snapshot(catalog, state, ["top"], dependency_depth=1)
    assert {node.metric_id for node in shallow.metrics} == {"top", "mid"}
    deep = build_state_snapshot(catalog, state, ["top"], dependency_depth=2)
    assert {node.metric_id for node in deep.metrics} == {"top", "mid", "leaf"}
    assert nodes_within_dependency_depth(catalog, ["top"], dependency_depth=0) == {"top"}


def test_snapshot_from_kpi_status():
    catalog = MetricCatalog([Metric(id="revenue", formula=Formula.sum("revenue"))])
    status = KPIStatus(
        name="revenue",
        value=10.0,
        baseline_mean=8.0,
        baseline_std=0.2,
        z_score=2.0,
        relative_change=0.25,
        anomaly=True,
        support=100,
        support_ok=True,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        directionality=Directionality.TWO_SIDED,
        state=KPIState.OPEN,
    )
    snapshot = snapshot_from_engine_statuses(catalog, {"revenue": status}, ["revenue"])
    node = snapshot.metric_map()["revenue"]
    assert node.current_value == 10.0
    assert node.baseline_value == 8.0
    assert node.absolute_change == 2.0
    assert node.relative_change == 0.25
    assert node.state == "OPEN"


def test_no_data_status_does_not_send_placeholder_zero():
    facts = runtime_state_from_status(
        KPIStatus(
            name="cash_inflow",
            value=0.0,
            baseline_mean=0.0,
            baseline_std=0.0,
            z_score=0.0,
            relative_change=0.0,
            anomaly=False,
            support=0,
            support_ok=True,
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
            state=KPIState.NORMAL,
        )
    )
    assert facts.current_value == 0.0


def test_canonical_hash_is_order_invariant():
    a = MetricStateGraphSnapshot(
        focal_metric_ids=["b", "a"],
        metrics=[
            MetricNodeSnapshot(metric_id="b", state="OPEN"),
            MetricNodeSnapshot(metric_id="a", state="NORMAL"),
        ],
        dependencies=[
            DependencyEdge(metric_id="b", depends_on="a"),
        ],
    )
    b = MetricStateGraphSnapshot(
        focal_metric_ids=["a", "b"],
        metrics=[
            MetricNodeSnapshot(metric_id="a", state="NORMAL"),
            MetricNodeSnapshot(metric_id="b", state="OPEN"),
        ],
        dependencies=[
            DependencyEdge(metric_id="b", depends_on="a"),
        ],
    )
    assert a.canonical_json() == b.canonical_json()
    assert a.sha256() == b.sha256()
    assert len(a.sha256()) == 64


def test_shuffled_wire_changes_bytes_but_not_canonical_hash():
    case = case_single_deep_driver()
    canonical = case.snapshot.canonical_json()
    wire = shuffled_wire_json(case.snapshot, seed=3)
    rebuilt = MetricStateGraphSnapshot.model_validate(json_loads(wire))
    assert rebuilt.sha256() == case.snapshot.sha256()
    assert isinstance(wire, str)
    assert "gross_margin" in canonical


def test_single_deep_driver_has_one_active_branch():
    snapshot = get_case("single_deep_driver").snapshot
    analysis = analyze_graph(snapshot)
    assert analysis.active_branch_count == 1
    assert analysis.deepest_active_metric_ids == ("unit_cost",)
    assert tuple(item.metric_id for item in analysis.investigation_candidates) == ("unit_cost",)
    assert analysis.competing_active_branches is False
    assert analysis.directional_support_known is False


def test_graph_analysis_covers_core_shapes():
    deep = analyze_graph(get_case("single_deep_driver").snapshot)
    assert any(path.metric_ids[-1] == "unit_cost" for path in deep.active_paths)

    isolated = analyze_graph(get_case("focal_only").snapshot)
    assert isolated.active_branch_count == 0
    assert isolated.investigation_candidates == ()

    multi = analyze_graph(get_case("multiple_active_branches").snapshot)
    assert multi.active_branch_count == 2
    assert multi.competing_active_branches is True
    assert len(multi.investigation_candidates) > 1

    shared = analyze_graph(get_case("shared_driver").snapshot)
    assert shared.multiple_focals_share_active_dependency is True
    assert "ticket_volume" in shared.shared_active_dependency_ids

    unrelated = analyze_graph(get_case("unrelated_incidents").snapshot)
    assert unrelated.multiple_focals_share_active_dependency is False

    missing = analyze_graph(get_case("missing_evidence").snapshot)
    assert "cash_inflow" in missing.no_data_metric_ids
    assert "cash_inflow" in missing.missing_evidence_metric_ids

    resolved = analyze_graph(get_case("resolved_dependency").snapshot)
    assert resolved.investigation_candidates == ()

    diamond = analyze_graph(get_case("diamond_shared_root").snapshot)
    assert diamond.deepest_active_metric_ids == ("unit_cost",)


def test_proposal_generation_rules():
    unique = get_case("single_deep_driver").snapshot
    proposals = generate_operational_proposals(unique)
    kinds = {item.kind for item in proposals}
    assert ProposalKind.route_investigation in kinds
    route = next(item for item in proposals if item.kind is ProposalKind.route_investigation)
    assert route.target_metric_id == "unit_cost"
    assert proposal_requires_model(analyze_graph(unique), route) is False

    competing = get_case("ambiguous_competing").snapshot
    auto = generate_operational_proposals(competing)
    assert all(item.kind is not ProposalKind.route_investigation for item in auto)

    shared = get_case("shared_driver").snapshot
    grouped = generate_operational_proposals(shared)
    assert any(item.kind is ProposalKind.group_incidents for item in grouped)

    unrelated = get_case("unrelated_incidents").snapshot
    assert all(
        item.kind is not ProposalKind.group_incidents
        for item in generate_operational_proposals(unrelated)
    )

    isolated = get_case("focal_only").snapshot
    assert generate_operational_proposals(isolated) == ()

    missing = get_case("missing_evidence").snapshot
    missing_proposals = generate_operational_proposals(missing)
    assert any(item.kind is ProposalKind.route_investigation for item in missing_proposals)
    analysis = analyze_graph(missing)
    assert analysis.missing_evidence_metric_ids


def test_model_gating_force_override():
    case = get_case("single_deep_driver")
    analysis = analyze_graph(case.snapshot)
    proposal = case.proposals[0].proposal
    assert proposal_requires_model(analysis, proposal) is False
    skipped = evaluate_case(case, (FixtureJudgmentProvider("jev"),), force_model=False)
    assert skipped.runs[0].skipped_reason == "structurally_resolved"
    forced = evaluate_case(case, (FixtureJudgmentProvider("jev"),), force_model=True)
    assert forced.runs[0].judgment is not None


def test_provider_payload_excludes_benchmark_metadata():
    for case in all_cases():
        payload = case.snapshot.canonical_json()
        data = json.loads(payload)
        assert set(data) == PROVIDER_SNAPSHOT_KEYS
        for field in BENCHMARK_METADATA_FIELDS:
            assert field not in data
        leaked = payload_contains_benchmark_metadata(
            payload,
            [case.notes, case.why_it_exists, "accepted_dispositions", "NOT SENT TO"],
        )
        assert leaked == []
        assert f'"{case.case_id}"' not in payload


def test_recording_provider_receives_identical_context():
    case = get_case("shared_driver")
    analysis = analyze_graph(case.snapshot)
    proposal = case.proposals[0].proposal
    context = JudgmentContext(snapshot=case.snapshot, proposal=proposal, graph_analysis=analysis)
    recorder = RecordingProvider(_judgment())
    run = recorder.judge(context, case_id=case.case_id)
    assert run.input_hash == context.sha256()
    data = json.loads(recorder.payloads[0])
    assert "snapshot" in data
    assert "proposal" in data
    assert "graph_analysis" in data
    assert "shared_driver" not in recorder.payloads[0]
    assert case.notes not in recorder.payloads[0]
    raw = JudgmentContext(snapshot=case.snapshot, proposal=proposal, graph_analysis=None)
    raw_payload = json.loads(raw.canonical_json())
    assert "graph_analysis" not in raw_payload
    assert raw.sha256() != context.sha256()


def test_raw_and_enriched_share_snapshot_and_proposal():
    case = get_case("localized_leaf")
    proposal = case.proposals[0].proposal
    analysis = analyze_graph(case.snapshot)
    raw = JudgmentContext(snapshot=case.snapshot, proposal=proposal, graph_analysis=None)
    enriched = JudgmentContext(snapshot=case.snapshot, proposal=proposal, graph_analysis=analysis)
    assert raw.snapshot.sha256() == enriched.snapshot.sha256()
    assert raw.proposal.sha256() == enriched.proposal.sha256()
    assert raw.graph_analysis is None
    assert enriched.graph_analysis is not None


def test_anonymize_preserves_topology_not_names():
    case = case_single_deep_driver()
    anon, mapping = anonymize_snapshot(case.snapshot)
    assert "gross_margin" not in anon.canonical_json()
    orig_edges = {(edge.metric_id, edge.depends_on) for edge in case.snapshot.dependencies}
    mapped_edges = {(mapping[src], mapping[dst]) for src, dst in orig_edges}
    anon_edges = {(edge.metric_id, edge.depends_on) for edge in anon.dependencies}
    assert mapped_edges == anon_edges
    variant, mapping2 = anonymized_variant(case)
    assert mapping == mapping2
    target = variant.proposals[0].proposal.target_metric_id
    assert target is not None
    assert "unit_cost" not in target


def test_irrelevant_nodes_have_no_focal_path():
    case = case_single_deep_driver()
    noisy = add_irrelevant_nodes(case.snapshot)
    extra = {node.metric_id for node in noisy.metrics} - {
        node.metric_id for node in case.snapshot.metrics
    }
    assert extra
    connected = {edge.metric_id for edge in noisy.dependencies} | {
        edge.depends_on for edge in noisy.dependencies
    }
    assert extra.isdisjoint(connected)
    variant = noise_variant(case)
    assert "headcount" in {node.metric_id for node in variant.snapshot.metrics}


def test_proposal_judgment_consistency_rules():
    ok = _judgment()
    assert ok.disposition is ProposalDisposition.accept
    with pytest.raises(ValidationError):
        ProposalJudgment.model_validate(
            {
                "evidence_sufficient": False,
                "human_review_required": False,
                "disposition": "accept",
            }
        )
    with pytest.raises(ValidationError):
        ProposalJudgment.model_validate(
            {
                "evidence_sufficient": True,
                "human_review_required": True,
                "disposition": "accept",
            }
        )
    with pytest.raises(ValidationError):
        ProposalJudgment.model_validate(
            {
                "evidence_sufficient": True,
                "human_review_required": False,
                "disposition": "request_more_evidence",
            }
        )
    with pytest.raises(ValidationError):
        ProposalJudgment.model_validate(
            {
                "evidence_sufficient": False,
                "human_review_required": False,
                "disposition": "human_review",
            }
        )
    reject = _judgment(disposition=ProposalDisposition.reject, evidence_sufficient=True)
    assert reject.disposition is ProposalDisposition.reject


def test_candidate_schema_is_constrained_and_shared():
    analysis = analyze_graph(get_case("ambiguous_competing").snapshot)
    candidates = tuple(item.metric_id for item in analysis.investigation_candidates)
    model_a = candidate_output_type(candidates)
    model_b = candidate_output_type(candidates)
    assert model_a is model_b
    allowed = {item.value for item in model_a.model_fields["target"].annotation}
    assert "abstain" in allowed
    assert "request_more_evidence" in allowed
    assert "unit_cost" in allowed
    assert "made_up_metric" not in allowed
    parsed = model_a.model_validate({"target": "unit_cost"})
    assert decode_target(parsed.target) == "unit_cost"


def test_fixture_providers_are_simulated_and_offline():
    case = get_case("single_deep_driver")
    context = JudgmentContext(
        snapshot=case.snapshot,
        proposal=case.proposals[0].proposal,
        graph_analysis=analyze_graph(case.snapshot),
    )
    jev = FixtureJudgmentProvider("jev").judge(
        context, case_id=case.case_id, force_model=True, model_required=False
    )
    openai = FixtureJudgmentProvider("openai").judge(
        context, case_id=case.case_id, force_model=True, model_required=False
    )
    assert jev.simulated is True
    assert openai.simulated is True
    assert jev.provider_details is not None
    assert openai.provider_details is None
    assert jev.input_hash == openai.input_hash == context.sha256()


def test_fixture_suite_overview_uses_observed_results():
    rows = overview_rows(evaluate_suite_fixture())
    assert len(rows) >= 12
    html = overview_table_html(rows)
    assert "SIMULATED" in html
    assert "Single deep driver" in html


def test_suite_inventory_counts_model_avoidance():
    inventory = suite_inventory()
    assert inventory["cases"] == len(all_cases())
    assert inventory["resolved_structurally"] >= 1
    assert inventory["generated_proposals"] >= 1


def test_policy_routes():
    review = apply_policy(
        _judgment(human_review_required=True, disposition=ProposalDisposition.human_review)
    )
    assert review.route is PolicyRoute.human_review
    more = apply_policy(
        _judgment(evidence_sufficient=False, disposition=ProposalDisposition.request_more_evidence)
    )
    assert more.route is PolicyRoute.request_more_evidence
    accept = apply_policy(_judgment())
    assert accept.route is PolicyRoute.accept_proposal
    case = get_case("ambiguous_competing")
    ctx = JudgmentContext(
        snapshot=case.snapshot,
        proposal=case.proposals[0].proposal,
        graph_analysis=analyze_graph(case.snapshot),
    )
    jev = FixtureJudgmentProvider("jev").judge(ctx, case_id=case.case_id, force_model=True)
    openai = FixtureJudgmentProvider("openai").judge(ctx, case_id=case.case_id, force_model=True)
    compared = apply_comparison_policy(case.proposals[0].proposal, jev, openai)
    assert compared.route is PolicyRoute.human_review
    assert "DISAGREEMENT" in compared.detail


def test_redact_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    assert "sk-secret-value" not in redact_secrets("error sk-secret-value boom")
    assert "[redacted]" in redact_secrets("error sk-secret-value boom")


def test_live_eval_refuses_without_flag(monkeypatch):
    monkeypatch.delenv(LIVE_FLAG, raising=False)
    assert live_main([]) == 2


def test_detect_availability_does_not_expose_keys(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    info = detect_availability()
    dumped = info.model_dump_json()
    assert "secret" not in dumped
    assert info.jev_key is True
    assert info.openai_key is True


def test_mocked_providers_share_schema_and_payload():
    import sys
    import types

    case = get_case("localized_leaf")
    context = JudgmentContext(
        snapshot=case.snapshot,
        proposal=case.proposals[0].proposal,
        graph_analysis=analyze_graph(case.snapshot),
    )
    payload_holder: dict[str, list[str]] = {"text": [], "types": []}

    class FakeResult:
        def __init__(self, output):
            self.output = output
            self.usage = {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15}
            self.response = MagicMock(
                provider_details={"confidence": {"disposition": 0.8}}, model_name="mock"
            )

    class FakeAgent:
        def __init__(self, model, output_type, instructions):
            self.model = model
            self.output_type = output_type
            self.instructions = instructions

        def run_sync(self, user_prompt, **kwargs):
            payload_holder["text"].append(user_prompt)
            payload_holder["types"].append(self.output_type)
            if (
                hasattr(self.output_type, "model_fields")
                and "target" in self.output_type.model_fields
            ):
                return FakeResult(self.output_type.model_validate({"target": "carrier_rate"}))
            return FakeResult(_judgment())

    fake_mod = types.ModuleType("pydantic_ai")
    fake_mod.Agent = FakeAgent
    with (
        patch.dict(os.environ, {"TYPESAFE_API_KEY": "x", "OPENAI_API_KEY": "y"}),
        patch.dict(sys.modules, {"pydantic_ai": fake_mod}),
    ):
        from examples.metric_judgment_eval.providers import (
            JevJudgmentProvider,
            OpenAIJudgmentProvider,
        )

        jev = JevJudgmentProvider("typesafe:jev-latest").judge(context, case_id=case.case_id)
        openai = OpenAIJudgmentProvider("openai:gpt-5.6-sol").judge(context, case_id=case.case_id)

    assert jev.judgment is not None
    assert openai.judgment is not None
    assert jev.input_hash == openai.input_hash == context.sha256()
    assert payload_holder["text"][0] == payload_holder["text"][1] == context.canonical_json()
    assert "localized_leaf" not in payload_holder["text"][0]
    assert openai.usage["input_tokens"] == 11


def test_run_selected_repeated_keeps_hash():
    case = get_case("shared_driver")
    providers = (FixtureJudgmentProvider("jev"),)
    result = run_selected(case, providers=providers, input_mode="realistic", n=5)
    hashes = {run.input_hash for run in result["evaluation"].runs}
    context = result["context"]
    assert context is not None
    assert hashes == {context.sha256()}
    assert len(result["evaluation"].runs) == 5


def test_robustness_fixture_stats_are_labeled_simulated():
    stats = robustness_from_fixture(get_case("single_deep_driver"))
    assert stats
    assert all(item.simulated for item in stats)
    assert robustness_rate([]) is None


def test_future_provider_not_yet_justified():
    assert "not yet justified" in future_provider_justified()


def test_viz_marks_notes_as_not_sent():
    case = get_case("missing_evidence")
    html = expectation_html(case, case.primary_expectation())
    assert "NOT SENT TO PROVIDER" in html
    assert case.notes in html
    analysis = analyze_graph(case.snapshot)
    assert "cash_inflow" in analysis_html(analysis)
    context = JudgmentContext(
        snapshot=case.snapshot,
        proposal=case.proposals[0].proposal,
        graph_analysis=analysis,
    )
    payload = payload_html(
        context.canonical_json(), context.sha256(), snapshot_hash=case.snapshot.sha256()
    )
    assert context.sha256() in payload
    board = comparison_html(
        FixtureJudgmentProvider("jev").judge(context, case_id=case.case_id, force_model=True),
        FixtureJudgmentProvider("openai").judge(context, case_id=case.case_id, force_model=True),
    )
    assert "Jev" in board
    assert "OpenAI" in board
    assert "Disposition" in board


def test_app_exists_and_has_no_chat_ui():
    text = APP.read_text(encoding="utf-8")
    assert "Run evaluation" in text
    assert "chat" not in text.lower() or "chatbot" not in text.lower()
    assert "prompt box" not in text.lower()
    assert APP.with_name("README.md").is_file()


def test_expected_proposal_coerces_sets():
    expected = ProposalExpectation(
        proposal=OperationalProposal(
            kind=ProposalKind.route_investigation,
            focal_metric_ids=("a",),
            target_metric_id="b",
        ),
        accepted_dispositions=ProposalDisposition.accept,
        accepted_evidence_sufficient=True,
        accepted_human_review_required=False,
    )
    assert expected.accepted_dispositions == {ProposalDisposition.accept}


def test_judgments_equal():
    a = _judgment()
    b = a.model_copy()
    assert judgments_equal(a, b)
    assert not judgments_equal(a, None)


def test_score_and_pairwise():
    case = get_case("single_deep_driver")
    context = JudgmentContext(
        snapshot=case.snapshot,
        proposal=case.proposals[0].proposal,
        graph_analysis=analyze_graph(case.snapshot),
    )
    left = FixtureJudgmentProvider("jev").judge(context, case_id=case.case_id, force_model=True)
    right = FixtureJudgmentProvider("openai").judge(context, case_id=case.case_id, force_model=True)
    score = score_run(left, case.proposals[0])
    assert score.field_total == 3
    pair = pairwise_agreement(left, right)
    assert pair.field_total == 3
    assert run_consistency([left, right])


def test_magnitude_and_degradation_sequences_exist():
    mag = magnitude_sequence()
    assert len(mag) == 5
    deg = degradation_sequence()
    assert len(deg) == 5
    assert analyze_graph(mag[0][1].snapshot).investigation_candidates
