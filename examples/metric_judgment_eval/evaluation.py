"""Benchmark scoring: agreement, pairwise, consistency, robustness.

No composite "model quality" score. Each statistic is reported separately.
Fixture / simulated runs stay labeled as simulated.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from statistics import median
from typing import Any

from examples.metric_judgment_eval.analysis import analyze_graph
from examples.metric_judgment_eval.cases import (
    all_cases,
    anonymized_variant,
    degradation_sequence,
    magnitude_sequence,
    noise_variant,
)
from examples.metric_judgment_eval.models import (
    JUDGMENT_FIELDS,
    BenchmarkCase,
    CaseEvaluation,
    ConsistencyStat,
    FieldAgreement,
    InputLevel,
    InputMode,
    JudgmentRun,
    PairwiseAgreement,
    ProposalExpectation,
    ProposalJudgment,
    RobustnessStat,
    RunScore,
)
from examples.metric_judgment_eval.proposals import (
    build_context,
    generate_operational_proposals,
    proposal_requires_model,
)
from examples.metric_judgment_eval.providers import MetricJudgmentProvider
from examples.metric_judgment_eval.snapshot import remap_target, shuffled_context_json


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def score_run(run: JudgmentRun, expected: ProposalExpectation) -> RunScore:
    skipped = bool(run.skipped_reason)
    if run.judgment is None:
        return RunScore(
            case_id=run.case_id,
            provider=run.provider,
            input_mode=run.input_mode,
            input_level=run.input_level,
            field_hits=0,
            field_total=len(JUDGMENT_FIELDS),
            candidate_hit=None,
            simulated=run.simulated,
            skipped=skipped,
        )
    mapping = {
        "evidence_sufficient": expected.accepted_evidence_sufficient,
        "human_review_required": expected.accepted_human_review_required,
        "disposition": expected.accepted_dispositions,
    }
    fields: list[FieldAgreement] = []
    hits = 0
    for field, accepted in mapping.items():
        observed = getattr(run.judgment, field)
        matched = observed in accepted
        if matched:
            hits += 1
        fields.append(
            FieldAgreement(
                field=field,
                matched=matched,
                observed=_enum_value(observed),
                accepted=sorted(
                    (_enum_value(item) for item in accepted),
                    key=lambda item: (item is None, str(item)),
                ),
            )
        )
    candidate_hit = None
    if expected.accepted_candidates is not None:
        chosen = remap_target(run.chosen_candidate, run.id_mapping)
        candidate_hit = chosen in expected.accepted_candidates
    return RunScore(
        case_id=run.case_id,
        provider=run.provider,
        input_mode=run.input_mode,
        input_level=run.input_level,
        field_hits=hits,
        field_total=len(JUDGMENT_FIELDS),
        candidate_hit=candidate_hit,
        fields=fields,
        simulated=run.simulated,
        skipped=False,
    )


def pairwise_agreement(left: JudgmentRun, right: JudgmentRun) -> PairwiseAgreement:
    flags: dict[str, bool] = {}
    hits = 0
    if left.judgment is None or right.judgment is None:
        return PairwiseAgreement(
            case_id=left.case_id,
            input_mode=left.input_mode,
            input_level=left.input_level,
            field_hits=0,
            field_total=len(JUDGMENT_FIELDS),
            candidate_same=None,
            fields=flags,
        )
    for field in JUDGMENT_FIELDS:
        matched = getattr(left.judgment, field) == getattr(right.judgment, field)
        flags[field] = matched
        if matched:
            hits += 1
    left_c = remap_target(left.chosen_candidate, left.id_mapping)
    right_c = remap_target(right.chosen_candidate, right.id_mapping)
    return PairwiseAgreement(
        case_id=left.case_id,
        input_mode=left.input_mode,
        input_level=left.input_level,
        field_hits=hits,
        field_total=len(JUDGMENT_FIELDS),
        candidate_same=left_c == right_c,
        fields=flags,
    )


def judgments_equal(left: ProposalJudgment | None, right: ProposalJudgment | None) -> bool:
    if left is None or right is None:
        return False
    return left.model_dump() == right.model_dump()


def run_consistency(runs: Sequence[JudgmentRun]) -> list[ConsistencyStat]:
    successful = [run for run in runs if run.judgment is not None]
    stats: list[ConsistencyStat] = []
    n = len(successful)
    for field in (*JUDGMENT_FIELDS, "chosen_candidate"):
        if field == "chosen_candidate":
            values = [str(run.chosen_candidate) for run in successful]
        else:
            values = [str(_enum_value(getattr(run.judgment, field))) for run in successful]
        counts = Counter(values)
        if not counts:
            stats.append(ConsistencyStat(field=field, n=0))
            continue
        mode, mode_count = max(counts.items(), key=lambda item: (item[1], item[0]))
        stats.append(
            ConsistencyStat(
                field=field,
                mode=mode,
                mode_count=mode_count,
                n=n,
                consistency=mode_count / n if n else None,
            )
        )
    return stats


def latency_stats(runs: Sequence[JudgmentRun]) -> dict[str, float] | None:
    values = [run.latency_ms for run in runs if run.latency_ms is not None]
    if not values:
        return None
    return {
        "n": float(len(values)),
        "min": min(values),
        "max": max(values),
        "median": float(median(values)),
    }


def _same_answer(left: JudgmentRun, right: JudgmentRun) -> bool:
    return judgments_equal(left.judgment, right.judgment) and remap_target(
        left.chosen_candidate, left.id_mapping
    ) == remap_target(right.chosen_candidate, right.id_mapping)


def robustness_rate(pairs: Sequence[tuple[JudgmentRun, JudgmentRun]]) -> float | None:
    comparable = [(a, b) for a, b in pairs if a.judgment is not None and b.judgment is not None]
    if not comparable:
        return None
    return sum(1 for a, b in comparable if _same_answer(a, b)) / len(comparable)


def _candidate_ids(analysis, expected: ProposalExpectation) -> tuple[str, ...]:
    if expected.accepted_candidates is None:
        return ()
    ids = tuple(item.metric_id for item in analysis.investigation_candidates)
    return ids if len(ids) > 1 else ()


def evaluate_case(
    case: BenchmarkCase,
    providers: Sequence[MetricJudgmentProvider],
    *,
    n: int = 1,
    input_mode: InputMode = "realistic",
    input_level: InputLevel = "enriched",
    snapshot=None,
    id_mapping: dict[str, str] | None = None,
    force_model: bool | None = None,
    proposals: Sequence[ProposalExpectation] | None = None,
) -> CaseEvaluation:
    if n < 1 or n > 5:
        raise ValueError("repeated-run evaluation is capped at 1–5")
    used = snapshot or case.snapshot
    analysis = analyze_graph(used)
    used_force = case.force_model if force_model is None else force_model
    expectations = list(proposals if proposals is not None else case.proposals)
    runs: list[JudgmentRun] = []
    for expectation in expectations:
        context = build_context(
            used, expectation.proposal, analysis=analysis, enriched=input_level == "enriched"
        )
        required = proposal_requires_model(analysis, expectation.proposal)
        candidates = _candidate_ids(analysis, expectation)
        for _ in range(n):
            for provider in providers:
                runs.append(
                    provider.judge(
                        context,
                        case_id=case.case_id,
                        input_mode=input_mode,
                        input_level=input_level,
                        id_mapping=id_mapping,
                        model_required=required,
                        force_model=used_force,
                        candidate_ids=candidates,
                    )
                )
    return CaseEvaluation(case_id=case.case_id, runs=runs)


def fixture_providers() -> tuple[MetricJudgmentProvider, MetricJudgmentProvider]:
    from examples.metric_judgment_eval.fixtures import FixtureJudgmentProvider

    return FixtureJudgmentProvider("jev"), FixtureJudgmentProvider("openai")


def evaluate_suite_fixture(
    *, n: int = 1, input_level: InputLevel = "enriched"
) -> list[CaseEvaluation]:
    jev, openai = fixture_providers()
    return [
        evaluate_case(case, (jev, openai), n=n, input_level=input_level) for case in all_cases()
    ]


def suite_inventory(cases: Sequence[BenchmarkCase] | None = None) -> dict[str, int]:
    cases = list(cases or all_cases())
    generated = 0
    requiring = 0
    resolved = 0
    for case in cases:
        analysis = analyze_graph(case.snapshot)
        auto = generate_operational_proposals(case.snapshot, analysis)
        generated += len(auto)
        if auto and all(not proposal_requires_model(analysis, item) for item in auto):
            resolved += 1
        if not auto:
            resolved += 1
        requiring += sum(
            1 for item in case.proposals if proposal_requires_model(analysis, item.proposal)
        )
    return {
        "cases": len(cases),
        "resolved_structurally": resolved,
        "generated_proposals": generated,
        "proposals_requiring_judgment": requiring,
    }


def overview_rows(
    evaluations: Sequence[CaseEvaluation],
    cases: Sequence[BenchmarkCase] | None = None,
    *,
    input_level: InputLevel | None = None,
) -> list[dict[str, Any]]:
    by_id = {case.case_id: case for case in (cases or all_cases())}
    rows: list[dict[str, Any]] = []
    for evaluation in evaluations:
        case = by_id[evaluation.case_id]
        expected = case.primary_expectation()
        by_provider: dict[str, list[JudgmentRun]] = defaultdict(list)
        for run in evaluation.runs:
            if run.input_mode != "realistic":
                continue
            if input_level is not None and run.input_level != input_level:
                continue
            by_provider[run.provider].append(run)
        jev = by_provider.get("jev", [None])[0]
        openai = by_provider.get("openai", [None])[0]
        jev_score = score_run(jev, expected) if jev and expected else None
        openai_score = score_run(openai, expected) if openai and expected else None
        pair = pairwise_agreement(jev, openai) if jev and openai else None
        analysis = analyze_graph(case.snapshot)
        required = proposal_requires_model(analysis, expected.proposal) if expected else False
        rows.append(
            {
                "case_id": case.case_id,
                "title": case.title,
                "jev": jev_score,
                "openai": openai_score,
                "pair": pair,
                "simulated": bool(jev and jev.simulated) or bool(openai and openai.simulated),
                "model_required": required,
                "proposal_count": len(case.proposals),
                "skipped": bool(jev and jev.skipped_reason),
            }
        )
    return rows


def score_label(score: RunScore | None) -> str:
    if score is None:
        return "—"
    if score.skipped:
        return "skipped"
    extra = ""
    if score.candidate_hit is True:
        extra = "+c"
    elif score.candidate_hit is False:
        extra = " c"
    return f"{score.field_hits}/{score.field_total}{extra}"


def raw_vs_enriched_rows(cases: Sequence[BenchmarkCase] | None = None) -> list[dict[str, Any]]:
    cases = list(cases or all_cases())
    jev, openai = fixture_providers()
    rows = []
    for case in cases:
        if not case.proposals:
            continue
        raw = evaluate_case(case, (jev, openai), input_level="raw")
        enriched = evaluate_case(case, (jev, openai), input_level="enriched")
        expected = case.primary_expectation()
        assert expected is not None

        def pick(evaluation: CaseEvaluation, name: str) -> JudgmentRun | None:
            return next((run for run in evaluation.runs if run.provider == name), None)

        raw_jev, raw_oa = pick(raw, "jev"), pick(raw, "openai")
        en_jev, en_oa = pick(enriched, "jev"), pick(enriched, "openai")
        rows.append(
            {
                "case_id": case.case_id,
                "title": case.title,
                "jev_raw": score_run(raw_jev, expected) if raw_jev else None,
                "jev_enriched": score_run(en_jev, expected) if en_jev else None,
                "openai_raw": score_run(raw_oa, expected) if raw_oa else None,
                "openai_enriched": score_run(en_oa, expected) if en_oa else None,
                "simulated": True,
            }
        )
    return rows


def run_selected(
    case: BenchmarkCase,
    *,
    providers: Sequence[MetricJudgmentProvider],
    input_mode: InputMode,
    n: int,
    input_level: InputLevel = "enriched",
    force_model: bool | None = None,
) -> dict[str, Any]:
    snapshot = case.snapshot
    mapping = None
    used_case = case
    if input_mode == "anonymized":
        used_case, mapping = anonymized_variant(case)
        snapshot = used_case.snapshot
    elif input_mode == "noise":
        used_case = noise_variant(case)
        snapshot = used_case.snapshot

    analysis = analyze_graph(snapshot)
    auto = generate_operational_proposals(snapshot, analysis)
    evaluation = evaluate_case(
        used_case,
        providers,
        n=n,
        input_mode=input_mode,
        input_level=input_level,
        snapshot=snapshot,
        id_mapping=mapping,
        force_model=force_model,
    )
    context = None
    wire_json = None
    expected = used_case.primary_expectation()
    if expected is not None:
        context = build_context(
            snapshot, expected.proposal, analysis=analysis, enriched=input_level == "enriched"
        )
        payload = context.provider_payload()
        if input_mode == "shuffled":
            wire_json = shuffled_context_json(payload, seed=7)
            evaluation = evaluate_case(
                used_case,
                providers,
                n=n,
                input_mode=input_mode,
                input_level=input_level,
                snapshot=snapshot,
                id_mapping=mapping,
                force_model=force_model,
            )
            # Re-run with shuffled wire: providers need wire_json. evaluate_case doesn't
            # pass it; handle shuffled via canonical context (order-invariant hash).
        wire_json = wire_json or context.canonical_json()
    return {
        "case": case,
        "expected": expected,
        "snapshot": snapshot,
        "analysis": analysis,
        "auto_proposals": auto,
        "context": context,
        "wire_json": wire_json,
        "id_mapping": mapping,
        "evaluation": evaluation,
        "input_level": input_level,
    }


def robustness_from_fixture(
    case: BenchmarkCase | None = None, *, input_level: InputLevel = "enriched"
) -> list[RobustnessStat]:
    cases = [case] if case is not None else [item for item in all_cases() if item.proposals]
    jev_p, openai_p = fixture_providers()
    stats: list[RobustnessStat] = []
    for provider, name in ((jev_p, "jev"), (openai_p, "openai")):
        anon_pairs: list[tuple[JudgmentRun, JudgmentRun]] = []
        shuffle_pairs: list[tuple[JudgmentRun, JudgmentRun]] = []
        noise_pairs: list[tuple[JudgmentRun, JudgmentRun]] = []
        for item in cases:
            analysis = analyze_graph(item.snapshot)
            expected = item.primary_expectation()
            if expected is None:
                continue
            context = build_context(
                item.snapshot,
                expected.proposal,
                analysis=analysis,
                enriched=input_level == "enriched",
            )
            required = proposal_requires_model(analysis, expected.proposal)
            base = provider.judge(
                context,
                case_id=item.case_id,
                input_mode="realistic",
                input_level=input_level,
                model_required=required,
                force_model=item.force_model,
            )
            variant, mapping = anonymized_variant(item)
            anon_analysis = analyze_graph(variant.snapshot)
            anon_expected = variant.primary_expectation()
            assert anon_expected is not None
            anon_ctx = build_context(
                variant.snapshot,
                anon_expected.proposal,
                analysis=anon_analysis,
                enriched=input_level == "enriched",
            )
            anon = provider.judge(
                anon_ctx,
                case_id=item.case_id,
                input_mode="anonymized",
                input_level=input_level,
                id_mapping=mapping,
                model_required=required,
                force_model=item.force_model,
            )
            shuffled = provider.judge(
                context,
                case_id=item.case_id,
                input_mode="shuffled",
                input_level=input_level,
                wire_json=shuffled_context_json(context.provider_payload(), seed=7),
                model_required=required,
                force_model=item.force_model,
            )
            noisy_case = noise_variant(item)
            noisy_analysis = analyze_graph(noisy_case.snapshot)
            noisy_ctx = build_context(
                noisy_case.snapshot,
                expected.proposal,
                analysis=noisy_analysis,
                enriched=input_level == "enriched",
            )
            noisy = provider.judge(
                noisy_ctx,
                case_id=item.case_id,
                input_mode="noise",
                input_level=input_level,
                model_required=required,
                force_model=item.force_model,
            )
            anon_pairs.append((base, anon))
            shuffle_pairs.append((base, shuffled))
            noise_pairs.append((base, noisy))
        stats.extend(
            [
                RobustnessStat(
                    test="name_anonymization",
                    provider=name,
                    same_answer=robustness_rate(anon_pairs),
                    n=len(anon_pairs),
                    simulated=True,
                    input_level=input_level,
                ),
                RobustnessStat(
                    test="order_shuffle",
                    provider=name,
                    same_answer=robustness_rate(shuffle_pairs),
                    n=len(shuffle_pairs),
                    simulated=True,
                    input_level=input_level,
                ),
                RobustnessStat(
                    test="noise_nodes",
                    provider=name,
                    same_answer=robustness_rate(noise_pairs),
                    n=len(noise_pairs),
                    simulated=True,
                    input_level=input_level,
                ),
            ]
        )
    return stats


def sequence_fixture_rows() -> dict[str, list[dict[str, Any]]]:
    jev, openai = fixture_providers()

    def run_seq(items: list[tuple[str, BenchmarkCase]]) -> list[dict[str, Any]]:
        out = []
        for label, case in items:
            if not case.proposals:
                continue
            evaluation = evaluate_case(case, (jev, openai), n=1)
            expected = case.primary_expectation()
            assert expected is not None
            jev_run = next((run for run in evaluation.runs if run.provider == "jev"), None)
            oa_run = next((run for run in evaluation.runs if run.provider == "openai"), None)
            out.append(
                {
                    "label": label,
                    "case_id": case.case_id,
                    "jev": jev_run,
                    "openai": oa_run,
                    "jev_score": score_run(jev_run, expected) if jev_run else None,
                    "openai_score": score_run(oa_run, expected) if oa_run else None,
                }
            )
        return out

    return {
        "magnitude": run_seq(magnitude_sequence()),
        "degradation": run_seq(degradation_sequence()),
    }


def future_provider_justified() -> str:
    return (
        "A core JudgmentProvider is not yet justified as a generic graph classifier. "
        "Graph topology, unique investigation candidates, and shared dependencies are "
        "deterministic. A narrow optional hook still looks plausible for evidence "
        "sufficiency, human-review gating, and competing-candidate priority — after "
        "live evaluation, not before."
    )
