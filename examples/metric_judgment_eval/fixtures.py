"""Hand-authored provider-shaped outputs for fixture mode.

These are simulated. They do not represent measured Jev or OpenAI behaviour.
"""

from __future__ import annotations

from typing import Any

from examples.metric_judgment_eval.models import (
    InputLevel,
    InputMode,
    JudgmentContext,
    JudgmentRun,
    ProposalJudgment,
    sha256_payload,
)
from examples.metric_judgment_eval.models import (
    ProposalDisposition as D,
)


def _judgment(**kwargs: Any) -> ProposalJudgment:
    return ProposalJudgment.model_validate(kwargs)


def _accept() -> ProposalJudgment:
    return _judgment(evidence_sufficient=True, human_review_required=False, disposition=D.accept)


def _reject() -> ProposalJudgment:
    return _judgment(evidence_sufficient=True, human_review_required=False, disposition=D.reject)


def _review(*, sufficient: bool = False) -> ProposalJudgment:
    return _judgment(
        evidence_sufficient=sufficient, human_review_required=True, disposition=D.human_review
    )


def _more_evidence() -> ProposalJudgment:
    return _judgment(
        evidence_sufficient=False, human_review_required=False, disposition=D.request_more_evidence
    )


# Authored disagreements. Simulated only. Neither provider is systematically better.
_JEV: dict[str, tuple[ProposalJudgment, str | None]] = {
    "single_deep_driver": (_accept(), None),
    "localized_leaf": (_accept(), None),
    "mixed_open_detected": (_accept(), None),
    "diamond_shared_root": (_accept(), None),
    "active_dependency_normal_parent": (_accept(), None),
    "shared_driver": (_accept(), None),
    "sibling_cluster": (_accept(), None),
    "missing_evidence": (_more_evidence(), None),
    "ambiguous_competing": (_review(), None),
    "multiple_active_branches": (_review(), None),
    "broad_systemic": (_review(), None),
    "offsetting_dependencies": (_reject(), None),
    "safe_parent_dedup": (_accept(), None),
    "unsafe_dedup": (_reject(), None),
    "grouping_superficial": (_reject(), None),
}

_OPENAI: dict[str, tuple[ProposalJudgment, str | None]] = {
    "single_deep_driver": (_accept(), None),
    "localized_leaf": (_accept(), None),
    "mixed_open_detected": (_accept(), None),
    "diamond_shared_root": (_accept(), None),
    "active_dependency_normal_parent": (_accept(), None),
    "shared_driver": (_accept(), None),
    "sibling_cluster": (_review(sufficient=True), None),
    "missing_evidence": (_review(), None),
    "ambiguous_competing": (_accept(), "unit_cost"),
    "multiple_active_branches": (_review(), "volume"),
    "broad_systemic": (_more_evidence(), None),
    "offsetting_dependencies": (_review(), None),
    "safe_parent_dedup": (_review(sufficient=True), None),
    "unsafe_dedup": (_review(), None),
    "grouping_superficial": (_reject(), None),
}


def _jev_details(judgment: ProposalJudgment) -> dict[str, Any]:
    confidence = {
        "evidence_sufficient": 0.81,
        "human_review_required": 0.74,
        "disposition": 0.79,
    }
    if judgment.disposition is D.human_review:
        confidence = {
            "evidence_sufficient": 0.56,
            "human_review_required": 0.88,
            "disposition": 0.84,
        }
    if judgment.disposition is D.request_more_evidence:
        confidence = {
            "evidence_sufficient": 0.91,
            "human_review_required": 0.60,
            "disposition": 0.86,
        }
    probabilities = {
        "disposition": {judgment.disposition.value: max(confidence["disposition"], 0.4)},
        "evidence_sufficient": {
            str(judgment.evidence_sufficient).lower(): confidence["evidence_sufficient"]
        },
    }
    return {"confidence": confidence, "probabilities": probabilities}


class FixtureJudgmentProvider:
    def __init__(self, provider: str) -> None:
        self.name = provider
        self.model = f"fixture:{provider}"

    def judge(
        self,
        context: JudgmentContext,
        *,
        case_id: str,
        input_mode: InputMode = "realistic",
        input_level: InputLevel = "enriched",
        wire_json: str | None = None,
        id_mapping: dict[str, str] | None = None,
        model_required: bool = True,
        force_model: bool = False,
        candidate_ids: tuple[str, ...] = (),
    ) -> JudgmentRun:
        del wire_json
        table = _JEV if self.name == "jev" else _OPENAI
        judgment, chosen = table.get(case_id, (_review(), None))
        if self.name == "openai" and input_mode != "realistic":
            # Anonymized/noise fixtures follow Jev's structural-leaning answers so
            # stability stats are defined, while remaining labeled simulated.
            judgment, chosen = _JEV.get(case_id, (_review(), None))
        if chosen is not None and id_mapping is not None:
            chosen = id_mapping.get(chosen, chosen)
        details = _jev_details(judgment) if self.name == "jev" else None
        payload = context.canonical_json()
        skipped = not model_required and not force_model
        return JudgmentRun(
            provider=self.name,
            model=self.model,
            case_id=case_id,
            input_hash=context.sha256(),
            snapshot_hash=context.snapshot.sha256(),
            proposal_hash=context.proposal.sha256(),
            input_mode=input_mode,
            input_level=input_level,
            proposal_kind=context.proposal.kind.value,
            judgment=None if skipped else judgment,
            chosen_candidate=None if skipped else chosen,
            candidate_metric_ids=list(candidate_ids),
            model_required=model_required,
            force_model=force_model,
            skipped_reason="structurally_resolved" if skipped else None,
            latency_ms=None if skipped else (12.0 if self.name == "jev" else 420.0),
            provider_details=None if skipped else details,
            simulated=True,
            validation_ok=True,
            wire_hash=sha256_payload(payload),
            id_mapping=id_mapping,
        )
