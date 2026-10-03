"""Reusable graph-pattern and operational-proposal benchmark cases.

Cases are structural, not stories. Provider input is only JudgmentContext.
Expected labels, notes, and category stay on the case object.
"""

from __future__ import annotations

from collections.abc import Sequence

from examples.metric_judgment_eval.analysis import analyze_graph
from examples.metric_judgment_eval.models import (
    BenchmarkCase,
    OperationalProposal,
    ProposalExpectation,
    ProposalKind,
)
from examples.metric_judgment_eval.models import (
    ProposalDisposition as D,
)
from examples.metric_judgment_eval.proposals import generate_operational_proposals
from examples.metric_judgment_eval.snapshot import (
    RuntimeNodeState,
    add_irrelevant_nodes,
    anonymize_snapshot,
    build_state_snapshot,
)
from metric_runtime import Directionality, Formula, Metric, MetricCatalog


def _metric(
    metric_id: str,
    deps: Sequence[str] = (),
    *,
    unit: str = "unit",
    directionality: Directionality = Directionality.TWO_SIDED,
) -> Metric:
    return Metric(
        id=metric_id,
        name=metric_id.replace("_", " ").title(),
        formula=Formula.sum(metric_id),
        dependencies=tuple(deps),
        unit=unit,
        directionality=directionality,
    )


def _facts(
    state: str,
    *,
    value: float | None,
    baseline: float | None,
    previous: float | None = None,
    quality: str | None = "value",
    relative: float | None = None,
) -> RuntimeNodeState:
    absolute = None
    if value is not None and baseline is not None:
        absolute = value - baseline
    if relative is None and value is not None and baseline not in (None, 0):
        relative = (value - baseline) / abs(baseline)
    if state == "NO_DATA":
        quality = "no_data"
        value = None
        baseline = None
        previous = None
        absolute = None
        relative = None
    return RuntimeNodeState(
        current_value=value,
        baseline_value=baseline,
        previous_value=previous,
        absolute_change=absolute,
        relative_change=relative,
        state=state,
        quality=quality,
    )


def _snapshot(
    case_id: str,
    metrics: Sequence[Metric],
    focals: Sequence[str],
    states: dict[str, RuntimeNodeState],
    *,
    depth: int = 4,
):
    catalog = MetricCatalog(metrics, name=case_id)
    return build_state_snapshot(catalog, states, focals, dependency_depth=depth)


def _expect(
    proposal: OperationalProposal,
    *,
    dispositions: set[D] | D,
    sufficient: set[bool] | bool,
    review: set[bool] | bool,
    candidates: set[str | None] | None = None,
) -> ProposalExpectation:
    return ProposalExpectation(
        proposal=proposal,
        accepted_dispositions=dispositions if isinstance(dispositions, set) else {dispositions},
        accepted_evidence_sufficient=sufficient if isinstance(sufficient, set) else {sufficient},
        accepted_human_review_required=review if isinstance(review, set) else {review},
        accepted_candidates=candidates,
    )


def _auto_kind(snapshot, kind: ProposalKind) -> OperationalProposal | None:
    analysis = analyze_graph(snapshot)
    for proposal in generate_operational_proposals(snapshot, analysis):
        if proposal.kind is kind:
            return proposal
    return None


def _case(
    case_id: str,
    title: str,
    category: str,
    notes: str,
    why: str,
    snapshot,
    proposals: Sequence[ProposalExpectation],
    *,
    force_model: bool = False,
) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        title=title,
        category=category,
        notes=notes,
        why_it_exists=why,
        snapshot=snapshot,
        proposals=tuple(proposals),
        force_model=force_model,
    )


def _authored(
    kind: ProposalKind,
    focals: Sequence[str],
    *,
    target: str | None = None,
    related: Sequence[str] = (),
    facts: Sequence[str] = (),
) -> OperationalProposal:
    return OperationalProposal(
        kind=kind,
        focal_metric_ids=tuple(focals),
        target_metric_id=target,
        related_metric_ids=tuple(related),
        rationale_facts=tuple(facts)
        or ("Authored proposal for the decision-focused benchmark — not model-generated.",),
    )


def case_single_deep_driver() -> BenchmarkCase:
    snapshot = _snapshot(
        "single_deep_driver",
        [
            _metric("gross_margin", ["revenue", "cost_of_goods"], unit="percent"),
            _metric("revenue", unit="EUR"),
            _metric("cost_of_goods", ["unit_cost", "volume"], unit="EUR"),
            _metric("unit_cost", unit="EUR"),
            _metric("volume", unit="count"),
        ],
        ["gross_margin"],
        {
            "gross_margin": _facts("OPEN", value=0.18, baseline=0.24, relative=-0.25),
            "revenue": _facts("NORMAL", value=100_000, baseline=99_000, relative=0.01),
            "cost_of_goods": _facts("OPEN", value=82_000, baseline=75_000, relative=0.093),
            "unit_cost": _facts("OPEN", value=8.2, baseline=7.4, relative=0.108),
            "volume": _facts("NORMAL", value=10_000, baseline=10_100, relative=-0.01),
        },
    )
    route = _auto_kind(snapshot, ProposalKind.route_investigation)
    assert route is not None and route.target_metric_id == "unit_cost"
    return _case(
        "single_deep_driver",
        "Single deep driver",
        "structure",
        "One active cost branch under an OPEN parent; revenue is quiet.",
        "Unique deepest candidate is a graph fact. Models only judge whether routing is warranted.",
        snapshot,
        [
            _expect(
                route,
                dispositions=D.accept,
                sufficient=True,
                review=False,
            )
        ],
        force_model=True,
    )


def case_focal_only() -> BenchmarkCase:
    snapshot = _snapshot(
        "focal_only",
        [
            _metric("conversion_rate", ["purchases", "sessions"], unit="percent"),
            _metric("purchases", unit="count"),
            _metric("sessions", unit="count"),
        ],
        ["conversion_rate"],
        {
            "conversion_rate": _facts("OPEN", value=0.021, baseline=0.034, relative=-0.38),
            "purchases": _facts("NORMAL", value=210, baseline=208, relative=0.01),
            "sessions": _facts("NORMAL", value=10_000, baseline=9_900, relative=0.01),
        },
    )
    return _case(
        "focal_only",
        "Focal only",
        "isolation",
        "Focal OPEN while both direct dependencies stay NORMAL.",
        "No automatic route proposal: there is no active descendant candidate.",
        snapshot,
        (),
    )


def case_multiple_active_branches() -> BenchmarkCase:
    snapshot = _snapshot(
        "multiple_active_branches",
        [
            _metric("operating_profit", ["revenue", "operating_cost"], unit="EUR"),
            _metric("revenue", ["volume", "average_price"], unit="EUR"),
            _metric("volume", unit="count"),
            _metric("average_price", unit="EUR"),
            _metric("operating_cost", ["labor_cost", "infrastructure_cost"], unit="EUR"),
            _metric("labor_cost", unit="EUR"),
            _metric("infrastructure_cost", unit="EUR"),
        ],
        ["operating_profit"],
        {
            "operating_profit": _facts("OPEN", value=12_000, baseline=28_000, relative=-0.57),
            "revenue": _facts("OPEN", value=80_000, baseline=95_000, relative=-0.16),
            "volume": _facts("OPEN", value=8_000, baseline=10_000, relative=-0.20),
            "average_price": _facts("NORMAL", value=10.0, baseline=9.5, relative=0.05),
            "operating_cost": _facts("OPEN", value=68_000, baseline=67_000, relative=0.015),
            "labor_cost": _facts("OPEN", value=40_000, baseline=36_000, relative=0.11),
            "infrastructure_cost": _facts("OPEN", value=28_000, baseline=24_000, relative=0.17),
        },
    )
    proposal = _authored(
        ProposalKind.route_investigation,
        ["operating_profit"],
        target="volume",
        facts=(
            "Authored routing to one of several active branches.",
            "GraphAnalysis reports competing active branches.",
        ),
    )
    return _case(
        "multiple_active_branches",
        "Multiple active branches",
        "structure",
        "Revenue and cost branches are both OPEN under operating profit.",
        "Routing to a single competing branch should not be accepted as unique.",
        snapshot,
        [
            _expect(
                proposal,
                dispositions={D.human_review, D.reject, D.request_more_evidence},
                sufficient={False, True},
                review={True, False},
                candidates={None, "volume", "labor_cost", "infrastructure_cost"},
            )
        ],
    )


def case_shared_driver() -> BenchmarkCase:
    snapshot = _snapshot(
        "shared_driver",
        [
            _metric("support_cost", ["ticket_volume"], unit="EUR"),
            _metric("support_sla", ["ticket_volume"], unit="percent"),
            _metric("ticket_volume", unit="count"),
        ],
        ["support_cost", "support_sla"],
        {
            "support_cost": _facts("OPEN", value=48_000, baseline=32_000, relative=0.50),
            "support_sla": _facts("OPEN", value=0.78, baseline=0.94, relative=-0.17),
            "ticket_volume": _facts("OPEN", value=4_200, baseline=2_100, relative=1.0),
        },
    )
    group = _auto_kind(snapshot, ProposalKind.group_incidents)
    assert group is not None
    return _case(
        "shared_driver",
        "Shared driver / incident grouping",
        "grouping",
        "Two OPEN focals share one OPEN dependency.",
        "Shared dependency is a graph fact. Models judge whether grouping is warranted.",
        snapshot,
        [_expect(group, dispositions=D.accept, sufficient=True, review=False)],
    )


def case_unrelated_incidents() -> BenchmarkCase:
    snapshot = _snapshot(
        "unrelated_incidents",
        [
            _metric("conversion_rate", ["purchases", "sessions"], unit="percent"),
            _metric("purchases", unit="count"),
            _metric("sessions", unit="count"),
            _metric("cloud_cost", ["compute_hours"], unit="EUR"),
            _metric("compute_hours", unit="count"),
        ],
        ["conversion_rate", "cloud_cost"],
        {
            "conversion_rate": _facts("OPEN", value=0.02, baseline=0.035, relative=-0.43),
            "purchases": _facts("OPEN", value=160, baseline=280, relative=-0.43),
            "sessions": _facts("NORMAL", value=8_000, baseline=8_000, relative=0.0),
            "cloud_cost": _facts("OPEN", value=22_000, baseline=12_000, relative=0.83),
            "compute_hours": _facts("OPEN", value=9_500, baseline=5_000, relative=0.90),
        },
    )
    return _case(
        "unrelated_incidents",
        "Two unrelated incidents",
        "grouping",
        "Two OPEN focals with disjoint active branches.",
        "Deterministic analysis must not propose grouping disjoint focals.",
        snapshot,
        (),
    )


def case_missing_evidence() -> BenchmarkCase:
    snapshot = _snapshot(
        "missing_evidence",
        [
            _metric("cash_balance", ["cash_inflow", "cash_outflow"], unit="EUR"),
            _metric("cash_inflow", unit="EUR"),
            _metric("cash_outflow", unit="EUR"),
        ],
        ["cash_balance"],
        {
            "cash_balance": _facts("OPEN", value=40_000, baseline=90_000, relative=-0.56),
            "cash_inflow": _facts("NO_DATA", value=None, baseline=None),
            "cash_outflow": _facts("OPEN", value=70_000, baseline=40_000, relative=0.75),
        },
    )
    route = _auto_kind(snapshot, ProposalKind.route_investigation)
    assert route is not None
    return _case(
        "missing_evidence",
        "Missing critical evidence",
        "quality",
        "A critical inflow dependency has NO_DATA while outflow is OPEN.",
        "Routing to the visible branch is proposed, but missing evidence should block accept.",
        snapshot,
        [
            _expect(
                route,
                dispositions={D.request_more_evidence, D.human_review},
                sufficient=False,
                review={True, False},
            )
        ],
    )


def case_offsetting_dependencies() -> BenchmarkCase:
    snapshot = _snapshot(
        "offsetting_dependencies",
        [
            _metric(
                "net_revenue_retention", ["expansion_revenue", "churned_revenue"], unit="percent"
            ),
            _metric("expansion_revenue", unit="EUR"),
            _metric("churned_revenue", unit="EUR"),
        ],
        ["net_revenue_retention"],
        {
            "net_revenue_retention": _facts("NORMAL", value=1.02, baseline=1.01, relative=0.01),
            "expansion_revenue": _facts("OPEN", value=40_000, baseline=18_000, relative=1.22),
            "churned_revenue": _facts("OPEN", value=38_000, baseline=16_000, relative=1.38),
        },
    )
    proposal = _authored(
        ProposalKind.route_investigation,
        ["net_revenue_retention"],
        target="churned_revenue",
        facts=("Authored routing of a NORMAL parent while two children are both OPEN.",),
    )
    return _case(
        "offsetting_dependencies",
        "Offsetting dependencies",
        "structure",
        "Expansion and churn are both OPEN; NRR stays NORMAL.",
        "Do not auto-route a quiet parent to one of two offsetting children.",
        snapshot,
        [
            _expect(
                proposal,
                dispositions={D.reject, D.human_review, D.request_more_evidence},
                sufficient={False, True},
                review={True, False},
            )
        ],
    )


def case_localized_leaf() -> BenchmarkCase:
    snapshot = _snapshot(
        "localized_leaf",
        [
            _metric("delivery_cost_per_order", ["delivery_cost", "orders"], unit="EUR"),
            _metric("delivery_cost", ["carrier_rate"], unit="EUR"),
            _metric("carrier_rate", unit="EUR"),
            _metric("orders", unit="count"),
        ],
        ["delivery_cost_per_order"],
        {
            "delivery_cost_per_order": _facts("OPEN", value=6.4, baseline=4.1, relative=0.56),
            "delivery_cost": _facts("OPEN", value=32_000, baseline=20_000, relative=0.60),
            "carrier_rate": _facts("OPEN", value=5.8, baseline=3.6, relative=0.61),
            "orders": _facts("NORMAL", value=5_000, baseline=4_900, relative=0.02),
        },
    )
    route = _auto_kind(snapshot, ProposalKind.route_investigation)
    assert route is not None
    return _case(
        "localized_leaf",
        "Localized leaf issue",
        "structure",
        "A deep carrier-rate leaf is OPEN; sibling orders stay NORMAL.",
        "Unique deepest candidate is carrier_rate.",
        snapshot,
        [_expect(route, dispositions=D.accept, sufficient=True, review=False)],
        force_model=True,
    )


def case_broad_systemic() -> BenchmarkCase:
    snapshot = _snapshot(
        "broad_systemic",
        [
            _metric("free_cash_flow", ["operating_cash_flow", "capital_expenditure"], unit="EUR"),
            _metric("operating_cash_flow", ["collections", "supplier_payments"], unit="EUR"),
            _metric("collections", unit="EUR"),
            _metric("supplier_payments", unit="EUR"),
            _metric("capital_expenditure", unit="EUR"),
        ],
        ["free_cash_flow"],
        {
            "free_cash_flow": _facts("OPEN", value=-20_000, baseline=15_000, relative=-2.33),
            "operating_cash_flow": _facts("OPEN", value=8_000, baseline=40_000, relative=-0.80),
            "collections": _facts("OPEN", value=50_000, baseline=80_000, relative=-0.38),
            "supplier_payments": _facts("OPEN", value=42_000, baseline=40_000, relative=0.05),
            "capital_expenditure": _facts("OPEN", value=28_000, baseline=12_000, relative=1.33),
        },
    )
    proposal = _authored(
        ProposalKind.route_investigation,
        ["free_cash_flow"],
        target="capital_expenditure",
        facts=("Authored routing to one branch of a systemic multi-branch incident.",),
    )
    return _case(
        "broad_systemic",
        "Broad systemic pattern",
        "structure",
        "Independent cash-flow and capex branches are OPEN under free cash flow.",
        "A systemic pattern is not a unique-route accept.",
        snapshot,
        [
            _expect(
                proposal,
                dispositions={D.human_review, D.reject, D.request_more_evidence},
                sufficient={False, True},
                review={True, False},
                candidates={None, "collections", "capital_expenditure", "supplier_payments"},
            )
        ],
    )


def case_active_dependency_normal_parent() -> BenchmarkCase:
    snapshot = _snapshot(
        "active_dependency_normal_parent",
        [
            _metric("customer_retention", ["cancellation_rate"], unit="percent"),
            _metric("cancellation_rate", unit="percent"),
        ],
        ["customer_retention"],
        {
            "customer_retention": _facts("NORMAL", value=0.91, baseline=0.92, relative=-0.01),
            "cancellation_rate": _facts("OPEN", value=0.09, baseline=0.05, relative=0.80),
        },
    )
    route = _auto_kind(snapshot, ProposalKind.route_investigation)
    assert route is not None
    return _case(
        "active_dependency_normal_parent",
        "Active dependency, normal parent",
        "over_escalation",
        "Cancellation rate is OPEN; customer retention remains NORMAL.",
        "Routing to the active child is a graph fact; the parent is not itself an incident.",
        snapshot,
        [_expect(route, dispositions=D.accept, sufficient=True, review=False)],
        force_model=True,
    )


def case_unexplained_graph() -> BenchmarkCase:
    snapshot = _snapshot(
        "unexplained_graph",
        [
            _metric("gross_profit", ["revenue", "cost_of_goods"], unit="EUR"),
            _metric("revenue", unit="EUR"),
            _metric("cost_of_goods", unit="EUR"),
        ],
        ["gross_profit"],
        {
            "gross_profit": _facts("OPEN", value=18_000, baseline=24_000, relative=-0.25),
            "revenue": _facts("NORMAL", value=100_400, baseline=100_000, relative=0.004),
            "cost_of_goods": _facts("NORMAL", value=82_200, baseline=82_000, relative=0.002),
        },
    )
    return _case(
        "unexplained_graph",
        "Conflicting / unexplained graph",
        "ambiguity",
        "Focal OPEN while both dependencies are NORMAL with tiny numeric moves.",
        "No active descendant exists, so no automatic route is generated.",
        snapshot,
        (),
    )


def case_ambiguous_competing() -> BenchmarkCase:
    snapshot = _snapshot(
        "ambiguous_competing",
        [
            _metric("profit_margin", ["average_price", "unit_cost"], unit="percent"),
            _metric("average_price", unit="EUR"),
            _metric("unit_cost", unit="EUR"),
        ],
        ["profit_margin"],
        {
            "profit_margin": _facts("OPEN", value=0.16, baseline=0.22, relative=-0.27),
            "average_price": _facts("DETECTED", value=9.1, baseline=10.0, relative=-0.09),
            "unit_cost": _facts("DETECTED", value=7.6, baseline=7.0, relative=0.086),
        },
    )
    proposal = _authored(
        ProposalKind.route_investigation,
        ["profit_margin"],
        target="unit_cost",
        facts=("Authored routing to one of two similar competing active branches.",),
    )
    return _case(
        "ambiguous_competing",
        "Ambiguous competing branches",
        "ambiguity",
        "Price and unit cost are both DETECTED with similar relative movement.",
        "Competing similar branches make unique auto-routing illegitimate.",
        snapshot,
        [
            _expect(
                proposal,
                dispositions={D.human_review, D.request_more_evidence, D.reject},
                sufficient=False,
                review={True, False},
                candidates={None, "average_price", "unit_cost"},
            )
        ],
    )


def case_diamond_shared_root() -> BenchmarkCase:
    snapshot = _snapshot(
        "diamond_shared_root",
        [
            _metric("contribution_profit", ["gross_profit", "variable_cost"], unit="EUR"),
            _metric("gross_profit", ["unit_cost"], unit="EUR"),
            _metric("variable_cost", ["unit_cost"], unit="EUR"),
            _metric("unit_cost", unit="EUR"),
        ],
        ["contribution_profit"],
        {
            "contribution_profit": _facts("OPEN", value=8_000, baseline=18_000, relative=-0.56),
            "gross_profit": _facts("OPEN", value=22_000, baseline=30_000, relative=-0.27),
            "variable_cost": _facts("OPEN", value=14_000, baseline=10_000, relative=0.40),
            "unit_cost": _facts("OPEN", value=7.4, baseline=5.5, relative=0.35),
        },
    )
    route = _auto_kind(snapshot, ProposalKind.route_investigation)
    assert route is not None
    return _case(
        "diamond_shared_root",
        "Diamond shared root",
        "grouping",
        "Two mid-nodes share one OPEN leaf under an OPEN parent (diamond).",
        "The unique deepest candidate is the shared leaf.",
        snapshot,
        [_expect(route, dispositions=D.accept, sufficient=True, review=False)],
        force_model=True,
    )


def case_sibling_cluster() -> BenchmarkCase:
    snapshot = _snapshot(
        "sibling_cluster",
        [
            _metric("support_cost", ["ticket_volume"], unit="EUR"),
            _metric("support_sla", ["ticket_volume"], unit="percent"),
            _metric("refund_rate", ["ticket_volume"], unit="percent"),
            _metric("ticket_volume", unit="count"),
        ],
        ["support_cost", "support_sla", "refund_rate"],
        {
            "support_cost": _facts("OPEN", value=51_000, baseline=30_000, relative=0.70),
            "support_sla": _facts("OPEN", value=0.74, baseline=0.95, relative=-0.22),
            "refund_rate": _facts("OPEN", value=0.08, baseline=0.03, relative=1.67),
            "ticket_volume": _facts("OPEN", value=5_000, baseline=2_000, relative=1.50),
        },
    )
    group = _auto_kind(snapshot, ProposalKind.group_incidents)
    assert group is not None
    return _case(
        "sibling_cluster",
        "Sibling alerts from one shared dependency",
        "grouping",
        "Three OPEN siblings share one OPEN ticket-volume driver.",
        "Shared active dependency is deterministic; grouping remains the judgment.",
        snapshot,
        [_expect(group, dispositions=D.accept, sufficient=True, review=False)],
    )


def case_resolved_dependency() -> BenchmarkCase:
    snapshot = _snapshot(
        "resolved_dependency",
        [
            _metric("order_cycle_time", ["warehouse_delay"], unit="count"),
            _metric("warehouse_delay", unit="count"),
        ],
        ["order_cycle_time"],
        {
            "order_cycle_time": _facts("OPEN", value=46.0, baseline=28.0, relative=0.64),
            "warehouse_delay": _facts("RESOLVED", value=12.0, baseline=12.0, relative=0.0),
        },
    )
    return _case(
        "resolved_dependency",
        "Resolved dependency under an OPEN parent",
        "state",
        "Parent remains OPEN after the explanatory child has RESOLVED.",
        "A resolved child is not an investigation candidate.",
        snapshot,
        (),
    )


def case_mixed_open_detected() -> BenchmarkCase:
    snapshot = _snapshot(
        "mixed_open_detected",
        [
            _metric("fulfillment_cost", ["pick_cost", "pack_cost"], unit="EUR"),
            _metric("pick_cost", unit="EUR"),
            _metric("pack_cost", unit="EUR"),
        ],
        ["fulfillment_cost"],
        {
            "fulfillment_cost": _facts("OPEN", value=19_000, baseline=14_000, relative=0.36),
            "pick_cost": _facts("DETECTED", value=12_500, baseline=9_000, relative=0.39),
            "pack_cost": _facts("NORMAL", value=6_400, baseline=6_300, relative=0.016),
        },
    )
    route = _auto_kind(snapshot, ProposalKind.route_investigation)
    assert route is not None
    return _case(
        "mixed_open_detected",
        "OPEN parent with a DETECTED leaf",
        "state",
        "A single DETECTED leaf under an OPEN parent; other siblings NORMAL.",
        "DETECTED still counts as an active investigation candidate.",
        snapshot,
        [_expect(route, dispositions=D.accept, sufficient=True, review=False)],
        force_model=True,
    )


def case_safe_parent_dedup() -> BenchmarkCase:
    snapshot = case_single_deep_driver().snapshot
    suppress = _auto_kind(snapshot, ProposalKind.suppress_redundant_notification)
    assert suppress is not None
    return _case(
        "safe_parent_dedup",
        "Safe parent deduplication",
        "decision",
        "Unique OPEN descendant already has an active incident; parent is derivative.",
        "Is the parent notification operationally redundant with the unique child incident?",
        snapshot,
        [
            _expect(
                suppress,
                dispositions={D.accept, D.human_review},
                sufficient={True, False},
                review={False, True},
            )
        ],
    )


def case_unsafe_dedup() -> BenchmarkCase:
    snapshot = case_multiple_active_branches().snapshot
    proposal = _authored(
        ProposalKind.suppress_redundant_notification,
        ["operating_profit"],
        target="volume",
        related=["volume"],
        facts=("Authored suppression of a parent that has two independent active branches.",),
    )
    return _case(
        "unsafe_dedup",
        "Unsafe parent deduplication",
        "decision",
        "Parent has two independent active branches; suppression cites only one.",
        "Dedup against a single branch should be rejected or reviewed.",
        snapshot,
        [
            _expect(
                proposal,
                dispositions={D.reject, D.human_review},
                sufficient={True, False},
                review={True, False},
            )
        ],
    )


def case_grouping_superficial() -> BenchmarkCase:
    snapshot = _snapshot(
        "grouping_superficial",
        [
            _metric("store_margin", ["store_revenue"], unit="percent"),
            _metric("store_revenue", unit="EUR"),
            _metric("warehouse_margin", ["warehouse_cost"], unit="percent"),
            _metric("warehouse_cost", unit="EUR"),
            _metric("company_overhead", unit="EUR"),
        ],
        ["store_margin", "warehouse_margin"],
        {
            "store_margin": _facts("OPEN", value=0.12, baseline=0.18, relative=-0.33),
            "store_revenue": _facts("OPEN", value=40_000, baseline=55_000, relative=-0.27),
            "warehouse_margin": _facts("OPEN", value=0.08, baseline=0.14, relative=-0.43),
            "warehouse_cost": _facts("OPEN", value=22_000, baseline=16_000, relative=0.38),
            "company_overhead": _facts("NORMAL", value=5_000, baseline=5_000, relative=0.0),
        },
    )
    proposal = _authored(
        ProposalKind.group_incidents,
        ["store_margin", "warehouse_margin"],
        related=["company_overhead"],
        facts=("Authored grouping of focals that share no active dependency.",),
    )
    return _case(
        "grouping_superficial",
        "Grouping with superficial proximity",
        "decision",
        "Two OPEN focals sit in the same slice but share no active dependency.",
        "Grouping without a shared active dependency should be rejected.",
        snapshot,
        [_expect(proposal, dispositions=D.reject, sufficient=True, review=False)],
    )


CASE_BUILDERS = (
    case_single_deep_driver,
    case_focal_only,
    case_multiple_active_branches,
    case_shared_driver,
    case_unrelated_incidents,
    case_missing_evidence,
    case_offsetting_dependencies,
    case_localized_leaf,
    case_broad_systemic,
    case_active_dependency_normal_parent,
    case_unexplained_graph,
    case_ambiguous_competing,
    case_diamond_shared_root,
    case_sibling_cluster,
    case_resolved_dependency,
    case_mixed_open_detected,
    case_safe_parent_dedup,
    case_unsafe_dedup,
    case_grouping_superficial,
)


def all_cases() -> list[BenchmarkCase]:
    return [builder() for builder in CASE_BUILDERS]


def get_case(case_id: str) -> BenchmarkCase:
    for case in all_cases():
        if case.case_id == case_id:
            return case
    raise KeyError(f"Unknown benchmark case: {case_id!r}")


def case_by_id() -> dict[str, BenchmarkCase]:
    return {case.case_id: case for case in all_cases()}


def noise_variant(case: BenchmarkCase) -> BenchmarkCase:
    return case.model_copy(update={"snapshot": add_irrelevant_nodes(case.snapshot)})


def anonymized_variant(case: BenchmarkCase) -> tuple[BenchmarkCase, dict[str, str]]:
    snapshot, mapping = anonymize_snapshot(case.snapshot)
    remapped: list[ProposalExpectation] = []
    for item in case.proposals:
        candidates = item.accepted_candidates
        if candidates is not None:
            candidates = {
                None if target is None else mapping.get(target, target) for target in candidates
            }
        remapped.append(
            item.model_copy(
                update={
                    "proposal": item.proposal.remapped(mapping),
                    "accepted_candidates": candidates,
                }
            )
        )
    variant = case.model_copy(update={"snapshot": snapshot, "proposals": tuple(remapped)})
    return variant, mapping


def magnitude_sequence() -> list[tuple[str, BenchmarkCase]]:
    """Same topology as single_deep_driver; unit_cost relative change varies."""
    base = case_single_deep_driver()
    nodes = base.snapshot.metric_map()
    unit = nodes["unit_cost"]
    variants: list[tuple[str, BenchmarkCase]] = []
    for rel in (0.02, 0.05, 0.10, 0.15, 0.25):
        current = (unit.baseline_value or 7.4) * (1 + rel)
        updated = [
            item
            if item.metric_id != "unit_cost"
            else item.model_copy(
                update={
                    "current_value": current,
                    "absolute_change": current - (unit.baseline_value or 7.4),
                    "relative_change": rel,
                }
            )
            for item in base.snapshot.metrics
        ]
        snapshot = base.snapshot.model_copy(update={"metrics": updated})
        route = _auto_kind(snapshot, ProposalKind.route_investigation)
        assert route is not None
        label = f"unit_cost {rel:+.0%}"
        variants.append(
            (
                label,
                base.model_copy(
                    update={
                        "case_id": f"magnitude_{rel:.2f}",
                        "snapshot": snapshot,
                        "proposals": (
                            _expect(route, dispositions=D.accept, sufficient=True, review=False),
                        ),
                        "title": f"Magnitude {label}",
                    }
                ),
            )
        )
    return variants


def degradation_sequence() -> list[tuple[str, BenchmarkCase]]:
    """Same routing proposal while evidence is progressively removed."""
    base = case_single_deep_driver()
    steps: list[tuple[str, BenchmarkCase]] = [("full_data", base)]

    def mutate(label: str, updater) -> None:
        snapshot = updater(base.snapshot)
        route = _auto_kind(snapshot, ProposalKind.route_investigation) or base.proposals[0].proposal
        steps.append(
            (
                label,
                base.model_copy(
                    update={
                        "case_id": f"degrade_{label}",
                        "snapshot": snapshot,
                        "title": f"Degradation {label}",
                        "proposals": (
                            _expect(
                                route,
                                dispositions={D.accept, D.human_review, D.request_more_evidence},
                                sufficient={True, False},
                                review={True, False},
                            ),
                        ),
                    }
                ),
            )
        )

    def drop_baseline(snapshot):
        metrics = [
            item.model_copy(
                update={"baseline_value": None, "relative_change": None, "absolute_change": None}
            )
            if item.metric_id == "unit_cost"
            else item
            for item in snapshot.metrics
        ]
        return snapshot.model_copy(update={"metrics": metrics})

    def drop_relative(snapshot):
        metrics = [
            item.model_copy(update={"relative_change": None})
            if item.metric_id == "unit_cost"
            else item
            for item in snapshot.metrics
        ]
        return snapshot.model_copy(update={"metrics": metrics})

    def competing_no_data(snapshot):
        metrics = [
            item.model_copy(
                update={
                    "state": "NO_DATA",
                    "quality": "no_data",
                    "current_value": None,
                    "baseline_value": None,
                    "relative_change": None,
                    "absolute_change": None,
                }
            )
            if item.metric_id == "revenue"
            else item
            for item in snapshot.metrics
        ]
        return snapshot.model_copy(update={"metrics": metrics})

    def drop_critical(snapshot):
        metrics = [
            item.model_copy(
                update={
                    "state": "NO_DATA",
                    "quality": "no_data",
                    "current_value": None,
                    "baseline_value": None,
                    "relative_change": None,
                    "absolute_change": None,
                }
            )
            if item.metric_id == "unit_cost"
            else item
            for item in snapshot.metrics
        ]
        return snapshot.model_copy(update={"metrics": metrics})

    mutate("no_baseline", drop_baseline)
    mutate("no_relative", drop_relative)
    mutate("competing_no_data", competing_no_data)
    mutate("critical_no_data", drop_critical)
    return steps
