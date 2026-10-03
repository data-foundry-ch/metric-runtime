"""Conservative operational-proposal generation from GraphAnalysis.

Python owns candidate generation. A judgment model is invoked only when the
proposal is operationally ambiguous, or when a case sets ``force_model=True``.
"""

from __future__ import annotations

from examples.metric_judgment_eval.analysis import analyze_graph, is_active
from examples.metric_judgment_eval.models import (
    GraphAnalysis,
    MetricStateGraphSnapshot,
    OperationalProposal,
    ProposalKind,
)


def _fmt_ids(values: tuple[str, ...] | list[str]) -> str:
    return ", ".join(values) if values else "none"


def unique_investigation_target(analysis: GraphAnalysis) -> str | None:
    if analysis.competing_active_branches:
        return None
    if len(analysis.investigation_candidates) != 1:
        return None
    return analysis.investigation_candidates[0].metric_id


def proposal_requires_model(analysis: GraphAnalysis, proposal: OperationalProposal) -> bool:
    """True when GraphAnalysis does not already settle the operational decision.

    Unique complete-data routing is structurally resolved (model_required=False).
    Grouping, suppression, missing evidence, and competing branches remain
    judgment questions.
    """
    if proposal.kind is ProposalKind.route_investigation:
        unique = unique_investigation_target(analysis)
        if (
            unique is not None
            and proposal.target_metric_id == unique
            and not analysis.missing_evidence_metric_ids
            and not analysis.no_data_metric_ids
        ):
            return False
        return True
    return True


def generate_operational_proposals(
    snapshot: MetricStateGraphSnapshot,
    analysis: GraphAnalysis | None = None,
) -> tuple[OperationalProposal, ...]:
    """Emit only proposals the graph can justify. Never invent a target."""
    analysis = analysis or analyze_graph(snapshot)
    nodes = snapshot.metric_map()
    proposals: list[OperationalProposal] = []

    if analysis.multiple_focals_share_active_dependency:
        proposals.append(
            OperationalProposal(
                kind=ProposalKind.group_incidents,
                focal_metric_ids=analysis.focal_metric_ids,
                related_metric_ids=analysis.shared_active_dependency_ids,
                rationale_facts=(
                    (
                        "Multiple focal metrics share active "
                        f"dependenc{'y' if len(analysis.shared_active_dependency_ids) == 1 else 'ies'} "
                        f"{_fmt_ids(analysis.shared_active_dependency_ids)}."
                    ),
                    "No model was used to detect the shared dependency.",
                ),
            )
        )
        return tuple(proposals)

    unique = unique_investigation_target(analysis)
    if unique is not None and len(analysis.focal_metric_ids) == 1:
        focal = analysis.focal_metric_ids[0]
        missing = analysis.missing_evidence_metric_ids
        facts = [
            f"Active dependency branch count is {analysis.active_branch_count}.",
            f"{unique} is the unique deepest active descendant of {focal}.",
        ]
        if missing:
            facts.append(f"Missing or NO_DATA observations: {_fmt_ids(missing)}.")
        else:
            facts.append("No dependency observations are missing.")
        if not analysis.directional_support_known:
            facts.append("Directional calculation support is unknown from current snapshot facts.")
        proposals.append(
            OperationalProposal(
                kind=ProposalKind.route_investigation,
                focal_metric_ids=(focal,),
                target_metric_id=unique,
                rationale_facts=tuple(facts),
            )
        )
        focal_node = nodes.get(focal)
        target_node = nodes.get(unique)
        if is_active(focal_node) and is_active(target_node) and unique != focal and not missing:
            proposals.append(
                OperationalProposal(
                    kind=ProposalKind.suppress_redundant_notification,
                    focal_metric_ids=(focal,),
                    target_metric_id=unique,
                    related_metric_ids=(unique,),
                    rationale_facts=(
                        f"{focal} is active and has a unique active descendant {unique}.",
                        "The descendant is already in an active incident state.",
                        "This evaluates a notification-dedup proposal only; it does not suppress anything.",
                    ),
                )
            )
        return tuple(proposals)

    return tuple(proposals)


def build_context(
    snapshot: MetricStateGraphSnapshot,
    proposal: OperationalProposal,
    *,
    analysis: GraphAnalysis | None = None,
    enriched: bool = True,
):
    from examples.metric_judgment_eval.models import JudgmentContext

    analysis = analysis or analyze_graph(snapshot)
    return JudgmentContext(
        snapshot=snapshot,
        proposal=proposal,
        graph_analysis=analysis if enriched else None,
    )
