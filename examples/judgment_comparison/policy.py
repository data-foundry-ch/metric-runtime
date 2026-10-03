"""Deterministic Python action policy.

Judgment models classify evidence. This module decides what the system may do.
No high-impact autonomous business action is allowed.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from examples.judgment_comparison.judgments import (
    BusinessJudgment,
    JudgmentRun,
    RecommendedAction,
    relevant_jev_confidence,
)

# Illustrative demo threshold — not a calibrated production gate.
JEV_CONFIDENCE_THRESHOLD = 0.70


class ActionRoute(str, Enum):
    no_action = "no_action"
    monitor = "monitor"
    human_review = "human_review"
    promotion_review = "promotion_review"
    cost_investigation = "cost_investigation"


class PolicyDecision(BaseModel):
    route: ActionRoute
    reason_code: str
    autonomous: bool = False
    detail: str = ""


_ACTION_ROUTES = {
    RecommendedAction.observe: (ActionRoute.monitor, "action_observe"),
    RecommendedAction.human_review: (ActionRoute.human_review, "action_human_review"),
    RecommendedAction.review_promotion: (ActionRoute.promotion_review, "action_review_promotion"),
    RecommendedAction.investigate_costs: (
        ActionRoute.cost_investigation,
        "action_investigate_costs",
    ),
    RecommendedAction.escalate_finance: (ActionRoute.human_review, "action_escalate_to_review"),
}


def apply_policy(
    judgment: BusinessJudgment,
    *,
    confidence_metadata: JudgmentRun | None = None,
    confidence_threshold: float = JEV_CONFIDENCE_THRESHOLD,
) -> PolicyDecision:
    """Conservative routing. Models never call tools or change runtime state."""
    if not judgment.material:
        return PolicyDecision(
            route=ActionRoute.monitor,
            reason_code="not_material",
            autonomous=False,
            detail="Change is not judged material. Continue monitoring.",
        )
    if judgment.needs_human_review:
        return PolicyDecision(
            route=ActionRoute.human_review,
            reason_code="needs_human_review",
            autonomous=False,
            detail="The judgment asked for human review before consequential action.",
        )
    if confidence_metadata is not None and confidence_metadata.provider == "jev":
        confidence = relevant_jev_confidence(confidence_metadata)
        if confidence is not None and confidence < confidence_threshold:
            return PolicyDecision(
                route=ActionRoute.human_review,
                reason_code="low_jev_confidence",
                autonomous=False,
                detail=(
                    f"Jev min field confidence {confidence:.2f} is below the illustrative "
                    f"{confidence_threshold:.2f} demo threshold."
                ),
            )
    route, reason = _ACTION_ROUTES[judgment.action]
    return PolicyDecision(
        route=route,
        reason_code=reason,
        autonomous=False,
        detail=f"Typed action {judgment.action.value} mapped to {route.value}.",
    )


DECISION_CRITICAL_FIELDS = ("primary_driver", "action", "needs_human_review")


def apply_comparison_policy(
    jev_run: JudgmentRun | None,
    llm_run: JudgmentRun | None,
    *,
    confidence_threshold: float = JEV_CONFIDENCE_THRESHOLD,
) -> PolicyDecision:
    """If both models returned judgments, disagreement on critical fields → review."""
    jev_judgment = jev_run.judgment if jev_run is not None else None
    llm_judgment = llm_run.judgment if llm_run is not None else None
    if jev_judgment is None and llm_judgment is None:
        return PolicyDecision(
            route=ActionRoute.human_review,
            reason_code="no_judgment",
            autonomous=False,
            detail="Neither provider returned a validated judgment.",
        )
    if jev_judgment is None:
        return apply_policy(llm_judgment, confidence_metadata=llm_run)
    if llm_judgment is None:
        return apply_policy(
            jev_judgment,
            confidence_metadata=jev_run,
            confidence_threshold=confidence_threshold,
        )
    disagreed = [
        field
        for field in DECISION_CRITICAL_FIELDS
        if getattr(jev_judgment, field) != getattr(llm_judgment, field)
    ]
    if disagreed:
        return PolicyDecision(
            route=ActionRoute.human_review,
            reason_code="model_disagreement",
            autonomous=False,
            detail="Decision-critical fields disagree: " + ", ".join(disagreed) + ".",
        )
    return apply_policy(
        jev_judgment,
        confidence_metadata=jev_run,
        confidence_threshold=confidence_threshold,
    )


def route_label(route: ActionRoute) -> str:
    return {
        ActionRoute.no_action: "NO ACTION",
        ActionRoute.monitor: "MONITOR",
        ActionRoute.human_review: "HUMAN REVIEW",
        ActionRoute.promotion_review: "PROMOTION REVIEW",
        ActionRoute.cost_investigation: "COST INVESTIGATION",
    }[route]
