"""Example-local experimental judgment providers.

Jev and OpenAI receive the same canonical JudgmentContext bytes and the same
Pydantic output schema. CI must never instantiate the live providers against
a network.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from math import isfinite
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, Field, create_model

from examples.metric_judgment_eval.models import (
    InputLevel,
    InputMode,
    JudgmentContext,
    JudgmentRun,
    ProposalJudgment,
)

DEFAULT_JEV_MODEL = "typesafe:jev-latest"
DEFAULT_OPENAI_MODEL = "openai:gpt-5.6-sol"
SHARED_INSTRUCTIONS = (
    "You are evaluating a proposed operational decision over a semantic metric "
    "runtime. All metric values, runtime states, dependency relationships, and "
    "graph-analysis facts supplied in the input were computed deterministically. "
    "Evaluate only whether the supplied evidence is sufficient for the proposed "
    "operation. Do not recompute graph structure. Do not invent external business "
    "context. If evidence is insufficient or ambiguous, prefer requesting more "
    "evidence or human review."
)
ABSTAIN_TARGET = "abstain"
REQUEST_MORE_EVIDENCE_TARGET = "request_more_evidence"
T = TypeVar("T")


class MetricJudgmentProvider(Protocol):
    name: str
    model: str

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
    ) -> JudgmentRun: ...


def _call_sync(fn: Callable[[], T]) -> T:
    """Run a blocking call even when marimo already has an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn).result()


def configured_jev_model() -> str:
    return os.environ.get("JEV_MODEL", DEFAULT_JEV_MODEL).strip() or DEFAULT_JEV_MODEL


def configured_openai_model() -> str:
    return os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_OPENAI_MODEL


class ProviderAvailability(BaseModel):
    pydantic_ai: bool
    jev_key: bool
    jev_model: str
    openai_key: bool
    openai_model: str

    @property
    def jev_ready(self) -> bool:
        return self.pydantic_ai and self.jev_key

    @property
    def openai_ready(self) -> bool:
        return self.pydantic_ai and self.openai_key


def detect_availability() -> ProviderAvailability:
    installed = False
    try:
        import pydantic_ai  # noqa: F401

        installed = True
    except ImportError:
        installed = False
    return ProviderAvailability(
        pydantic_ai=installed,
        jev_key=bool(os.environ.get("TYPESAFE_API_KEY")),
        jev_model=configured_jev_model(),
        openai_key=bool(os.environ.get("OPENAI_API_KEY")),
        openai_model=configured_openai_model(),
    )


def _secret_env_names() -> tuple[str, ...]:
    return (
        "TYPESAFE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "COHERE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
    )


def redact_secrets(text: str) -> str:
    cleaned = text
    for name in _secret_env_names():
        value = os.environ.get(name)
        if value:
            cleaned = cleaned.replace(value, "[redacted]")
    return cleaned


@lru_cache(maxsize=64)
def candidate_output_type(candidate_ids: tuple[str, ...]) -> type[BaseModel]:
    """Constrained choice among GraphAnalysis candidates plus abstain / more evidence."""
    members: dict[str, str] = {
        ABSTAIN_TARGET: ABSTAIN_TARGET,
        REQUEST_MORE_EVIDENCE_TARGET: REQUEST_MORE_EVIDENCE_TARGET,
    }
    for metric_id in candidate_ids:
        members[metric_id] = metric_id
    target_enum = Enum("InvestigationCandidateId", members, type=str)  # type: ignore[misc]
    return create_model(
        "InvestigationCandidateChoice",
        __doc__=(
            "Given these already-selected deterministic candidates, which candidate "
            "should receive investigation priority, or should the system abstain?"
        ),
        target=(
            target_enum,
            Field(
                description=(
                    "Given these already-selected deterministic candidates, which "
                    "candidate should receive investigation priority? Use abstain "
                    "when no unique priority is justified. Use request_more_evidence "
                    "when the candidate set is incomplete."
                )
            ),
        ),
    )


def decode_target(value: Any) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw)
    if text in {"", ABSTAIN_TARGET, "None", "null"}:
        return None
    return text


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _usage_dict(result: Any) -> dict[str, Any] | None:
    raw = getattr(result, "usage", None)
    if callable(raw):
        try:
            raw = raw()
        except TypeError:
            raw = None
    if raw is None:
        response = getattr(result, "response", None)
        raw = getattr(response, "usage", None) if response is not None else None
    if raw is None:
        return None
    if hasattr(raw, "model_dump"):
        dumped = raw.model_dump()
        return {
            key: _jsonable(value) for key, value in dumped.items() if value not in (None, {}, [])
        }
    if isinstance(raw, dict):
        return {key: _jsonable(value) for key, value in raw.items() if value not in (None, {}, [])}
    out: dict[str, Any] = {}
    for key in (
        "request_tokens",
        "response_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "requests",
        "cost",
    ):
        if hasattr(raw, key):
            value = getattr(raw, key)
            if value is not None:
                out[key] = _jsonable(value)
    return out or None


def _provider_details(result: Any) -> dict[str, Any] | None:
    response = getattr(result, "response", None)
    details = getattr(response, "provider_details", None) if response is not None else None
    if isinstance(details, dict) and details:
        return details
    return None


def _result_model_name(result: Any, fallback: str) -> str:
    response = getattr(result, "response", None)
    name = getattr(response, "model_name", None) if response is not None else None
    if isinstance(name, str) and name.strip():
        return name
    return fallback


def jev_confidence_map(details: dict[str, Any] | None) -> dict[str, float]:
    """Per-field Jev confidence. A margin, not P(answer is correct)."""
    if not details:
        return {}
    raw = details.get("confidence")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if isfinite(number):
            out[str(key)] = number
    return out


def jev_probabilities_map(details: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    if not details:
        return {}
    raw = details.get("probabilities")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for field, dist in raw.items():
        if not isinstance(dist, dict):
            continue
        parsed: dict[str, float] = {}
        for key, value in dist.items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if isfinite(number):
                parsed[str(key)] = number
        if parsed:
            out[str(field)] = parsed
    return out


def jev_scores_map(details: dict[str, Any] | None) -> dict[str, float]:
    if not details:
        return {}
    raw = details.get("scores")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if isfinite(number):
            out[str(key)] = number
    return out


def min_jev_confidence(details: dict[str, Any] | None) -> float | None:
    values = list(jev_confidence_map(details).values())
    if not values:
        return None
    return min(values)


def openai_token_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {}
    out: dict[str, int] = {}
    mapping = {
        "input_tokens": ("input_tokens", "request_tokens"),
        "output_tokens": ("output_tokens", "response_tokens"),
        "total_tokens": ("total_tokens",),
    }
    for label, keys in mapping.items():
        for key in keys:
            raw = usage.get(key)
            if isinstance(raw, bool) or raw is None:
                continue
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue
            if number >= 0:
                out[label] = number
                break
    return out


def _run_structured(
    model: str,
    user_text: str,
    output_type: type[BaseModel],
) -> tuple[Any, dict[str, Any] | None, dict[str, Any] | None, str, float, str | None]:
    from pydantic_ai import Agent

    agent = Agent(model, output_type=output_type, instructions=SHARED_INSTRUCTIONS)
    started = time.perf_counter()
    try:
        result = _call_sync(lambda: agent.run_sync(user_text))
    except Exception as exc:  # noqa: BLE001 — surface provider failures in the harness
        latency = (time.perf_counter() - started) * 1000.0
        return (
            None,
            None,
            None,
            model,
            round(latency, 1),
            redact_secrets(f"{type(exc).__name__}: {exc}"),
        )
    latency = (time.perf_counter() - started) * 1000.0
    return (
        getattr(result, "output", None),
        _usage_dict(result),
        _provider_details(result),
        _result_model_name(result, model),
        round(latency, 1),
        None,
    )


def _validate_judgment(output: Any) -> tuple[ProposalJudgment | None, str | None, bool]:
    if isinstance(output, ProposalJudgment):
        return output, None, True
    try:
        return ProposalJudgment.model_validate(output), None, True
    except Exception as exc:  # noqa: BLE001
        return None, redact_secrets(f"schema validation failed: {exc}"), False


def _base_run(
    *,
    provider: str,
    model: str,
    case_id: str,
    context: JudgmentContext,
    input_mode: InputMode,
    input_level: InputLevel,
    id_mapping: dict[str, str] | None,
    model_required: bool,
    force_model: bool,
    candidate_ids: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "case_id": case_id,
        "input_hash": context.sha256(),
        "snapshot_hash": context.snapshot.sha256(),
        "proposal_hash": context.proposal.sha256(),
        "input_mode": input_mode,
        "input_level": input_level,
        "proposal_kind": context.proposal.kind.value,
        "candidate_metric_ids": list(candidate_ids),
        "model_required": model_required,
        "force_model": force_model,
        "id_mapping": id_mapping,
    }


class LiveStructuredProvider:
    name: str = "live"

    def __init__(self, model: str, *, provider: str, required_env: str) -> None:
        self.model = model
        self.name = provider
        self.required_env = required_env

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
        base = _base_run(
            provider=self.name,
            model=self.model,
            case_id=case_id,
            context=context,
            input_mode=input_mode,
            input_level=input_level,
            id_mapping=id_mapping,
            model_required=model_required,
            force_model=force_model,
            candidate_ids=candidate_ids,
        )
        from examples.metric_judgment_eval.models import sha256_payload

        payload = wire_json if wire_json is not None else context.canonical_json()
        base["wire_hash"] = sha256_payload(payload)
        if not model_required and not force_model:
            return JudgmentRun(
                **base,
                skipped_reason="structurally_resolved",
                validation_ok=True,
            )
        if not os.environ.get(self.required_env):
            return JudgmentRun(**base, error=f"{self.required_env} is not configured")
        judgment_out, usage, details, model_name, latency_a, error_a = _run_structured(
            self.model, payload, ProposalJudgment
        )
        judgment, error_b, validation_ok = (
            (None, error_a, False) if error_a else _validate_judgment(judgment_out)
        )
        chosen = None
        candidate_usage = None
        candidate_details = None
        latency_b = 0.0
        error_c = None
        if error_a is None and error_b is None and len(candidate_ids) > 1:
            target_type = candidate_output_type(candidate_ids)
            target_out, candidate_usage, candidate_details, model_name_b, latency_b, error_c = (
                _run_structured(self.model, payload, target_type)
            )
            model_name = model_name_b or model_name
            if error_c is None:
                raw_target = getattr(target_out, "target", target_out)
                chosen = decode_target(raw_target)
                if (
                    chosen is not None
                    and chosen not in candidate_ids
                    and chosen != REQUEST_MORE_EVIDENCE_TARGET
                ):
                    error_c = f"candidate {chosen!r} is not in {list(candidate_ids)}"
                    chosen = None
                    validation_ok = False
        return JudgmentRun(
            provider=self.name,
            model=model_name,
            case_id=case_id,
            input_hash=base["input_hash"],
            snapshot_hash=base["snapshot_hash"],
            proposal_hash=base["proposal_hash"],
            input_mode=input_mode,
            input_level=input_level,
            proposal_kind=base["proposal_kind"],
            judgment=judgment,
            chosen_candidate=chosen,
            candidate_metric_ids=list(candidate_ids),
            model_required=model_required,
            force_model=force_model,
            latency_ms=round(latency_a + latency_b, 1),
            provider_details=details,
            usage=usage,
            candidate_provider_details=candidate_details,
            candidate_usage=candidate_usage,
            error=error_a or error_b or error_c,
            wire_hash=base["wire_hash"],
            id_mapping=id_mapping,
            validation_ok=validation_ok if error_a is None else False,
        )


class JevJudgmentProvider(LiveStructuredProvider):
    def __init__(self, model: str | None = None) -> None:
        super().__init__(
            model or configured_jev_model(),
            provider="jev",
            required_env="TYPESAFE_API_KEY",
        )


class OpenAIJudgmentProvider(LiveStructuredProvider):
    def __init__(self, model: str | None = None) -> None:
        super().__init__(
            model or configured_openai_model(),
            provider="openai",
            required_env="OPENAI_API_KEY",
        )


class RecordingProvider:
    """Test double that records the exact payload bytes."""

    name = "recording"
    model = "recording"

    def __init__(self, judgment: ProposalJudgment, chosen: str | None = None) -> None:
        self.judgment = judgment
        self.chosen = chosen
        self.payloads: list[str] = []
        self.output_types: list[str] = []

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
        payload = wire_json if wire_json is not None else context.canonical_json()
        self.payloads.append(payload)
        self.output_types.append(ProposalJudgment.__name__)
        from examples.metric_judgment_eval.models import sha256_payload

        return JudgmentRun(
            **_base_run(
                provider=self.name,
                model=self.model,
                case_id=case_id,
                context=context,
                input_mode=input_mode,
                input_level=input_level,
                id_mapping=id_mapping,
                model_required=model_required,
                force_model=force_model,
                candidate_ids=candidate_ids,
            ),
            judgment=self.judgment,
            chosen_candidate=self.chosen,
            latency_ms=0.0,
            validation_ok=True,
            wire_hash=sha256_payload(payload),
        )


def payload_contains_benchmark_metadata(payload: str, forbidden: Sequence[str]) -> list[str]:
    """Return forbidden substrings that leaked into a provider payload."""
    return [item for item in forbidden if item and item in payload]
