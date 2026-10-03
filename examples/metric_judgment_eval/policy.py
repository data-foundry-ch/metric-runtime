"""Illustrative deterministic policy after proposal judgment.

Judgment models evaluate a proposed operation. This module only routes.
It does not suppress, close, or mutate Metric Runtime incidents.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from examples.metric_judgment_eval.models import (
    JudgmentRun,
    OperationalProposal,
    ProposalDisposition,
    ProposalJudgment,
)

# Illustrative only — not a calibrated production threshold.
JEV_CONFIDENCE_THRESHOLD = 0.70


class PolicyRoute(str, Enum):
    human_review = "HUMAN_REVIEW"
    request_more_evidence = "REQUEST_MORE_EVIDENCE"
    accept_proposal = "ACCEPT_PROPOSAL"
    reject_proposal = "REJECT_PROPOSAL"


class PolicyDecision(BaseModel):
    route: PolicyRoute
    reason_code: str
    autonomous: bool = False
    detail: str = ""


def apply_policy(
    judgment: ProposalJudgment,
    *,
    jev_confidence: float | None = None,
) -> PolicyDecision:
    """facts → proposal → judgment → policy. Never closes a real incident."""
    if judgment.human_review_required or judgment.disposition is ProposalDisposition.human_review:
        return PolicyDecision(
            route=PolicyRoute.human_review,
            reason_code="human_review",
            detail="Judgment marked the proposed operation as needing human review.",
        )
    if judgment.disposition is ProposalDisposition.request_more_evidence:
        return PolicyDecision(
            route=PolicyRoute.request_more_evidence,
            reason_code="request_more_evidence",
            detail="Supplied evidence is not sufficient for the proposed operation.",
        )
    confident = jev_confidence is None or jev_confidence >= JEV_CONFIDENCE_THRESHOLD
    if not confident:
        return PolicyDecision(
            route=PolicyRoute.human_review,
            reason_code="low_confidence",
            detail="Illustrative Jev confidence threshold was not met.",
        )
    if judgment.disposition is ProposalDisposition.accept:
        return PolicyDecision(
            route=PolicyRoute.accept_proposal,
            reason_code="accept_proposal",
            autonomous=True,
            detail="Both evidence sufficiency and disposition support applying the proposal.",
        )
    return PolicyDecision(
        route=PolicyRoute.reject_proposal,
        reason_code="reject_proposal",
        detail="The proposed operation is not supported by the supplied evidence.",
    )


def apply_comparison_policy(
    proposal: OperationalProposal,
    jev_run: JudgmentRun | None,
    openai_run: JudgmentRun | None,
) -> PolicyDecision:
    del proposal
    runs = [run for run in (jev_run, openai_run) if run is not None and run.judgment is not None]
    if not runs:
        return PolicyDecision(
            route=PolicyRoute.human_review,
            reason_code="no_judgment",
            detail="No provider judgment is available.",
        )
    if len(runs) == 1:
        details = runs[0].provider_details if runs[0].provider == "jev" else None
        from examples.metric_judgment_eval.providers import min_jev_confidence

        return apply_policy(runs[0].judgment, jev_confidence=min_jev_confidence(details))

    left, right = runs[0].judgment, runs[1].judgment
    if left.human_review_required or right.human_review_required:
        return PolicyDecision(
            route=PolicyRoute.human_review,
            reason_code="either_human_review",
            detail="MODEL DISAGREEMENT → deterministic comparison policy chooses HUMAN REVIEW"
            if left.human_review_required != right.human_review_required
            else "A provider required human review.",
        )
    if left.disposition != right.disposition:
        return PolicyDecision(
            route=PolicyRoute.human_review,
            reason_code="disposition_disagreement",
            detail="MODEL DISAGREEMENT → deterministic comparison policy chooses HUMAN REVIEW",
        )
    if left.disposition is ProposalDisposition.request_more_evidence:
        return PolicyDecision(
            route=PolicyRoute.request_more_evidence,
            reason_code="both_request_more_evidence",
            detail="Both providers requested more evidence.",
        )
    if left.disposition is ProposalDisposition.accept:
        return PolicyDecision(
            route=PolicyRoute.accept_proposal,
            reason_code="both_accept",
            autonomous=False,
            detail="Both providers accepted. Still illustrative — not a production action.",
        )
    return PolicyDecision(
        route=PolicyRoute.reject_proposal,
        reason_code="both_reject",
        detail="Both providers rejected the proposed operation.",
    )
