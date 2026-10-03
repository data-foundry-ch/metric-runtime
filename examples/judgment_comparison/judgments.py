"""Shared Pydantic judgment contract, providers, fixtures, and comparison math.

Jev and the classic LLM receive identical evidence and return this same model.
Neither provider writes Metric Runtime state or chooses consequential actions.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from enum import Enum
from math import isfinite
from statistics import median
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, Field

JUDGMENT_FIELDS = (
    "material",
    "promotion_related",
    "primary_driver",
    "needs_human_review",
    "action",
)

DEFAULT_JEV_MODEL = "typesafe:jev-latest"
SHARED_INSTRUCTIONS = (
    "Judge only the supplied structured business evidence. "
    "Do not recompute metrics, percentages, or graph dependencies. "
    "Fill each output field from the evidence alone."
)

ProviderName = Literal["jev", "llm"]
T = TypeVar("T")


def _call_sync(fn: Callable[[], T]) -> T:
    """Run a blocking call even when marimo already has an event loop.

    ``Agent.run_sync()`` uses ``asyncio.run()``, which fails with
    ``RuntimeError: This event loop is already running``. A worker thread
    gets its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn).result()


class BusinessDriver(str, Enum):
    """Which predefined business mechanism best characterizes the evidence."""

    promotion_behavior = "promotion_behavior"
    volume = "volume"
    pricing = "pricing"
    cost_pressure = "cost_pressure"
    product_mix = "product_mix"
    data_quality = "data_quality"
    unclear = "unclear"


class RecommendedAction(str, Enum):
    """Which predefined next-action category fits the evidence."""

    observe = "observe"
    human_review = "human_review"
    review_promotion = "review_promotion"
    investigate_costs = "investigate_costs"
    escalate_finance = "escalate_finance"


class BusinessJudgment(BaseModel):
    """Typed business decision contract used by both Jev and the classic LLM.

    Each field is one evaluative question. There is no free-text explanation.
    """

    material: bool = Field(
        description=(
            "Is the observed change materially important enough that the "
            "business should pay attention?"
        )
    )
    promotion_related: bool = Field(
        description=(
            "Is the supplied evidence consistent with the active promotion "
            "being a meaningful driver of the observed deterioration?"
        )
    )
    primary_driver: BusinessDriver = Field(
        description=(
            "Which predefined business mechanism best characterizes the supplied evidence?"
        )
    )
    needs_human_review: bool = Field(
        description=(
            "Should a human review this incident before any consequential business action is taken?"
        )
    )
    action: RecommendedAction = Field(
        description=(
            "Which predefined next action is most appropriate given only the supplied evidence?"
        )
    )


class JudgmentRun(BaseModel):
    provider: str
    model: str
    judgment: BusinessJudgment | None = None
    latency_ms: float | None = None
    usage: dict[str, Any] | None = None
    provider_details: dict[str, Any] | None = None
    cost_usd: float | None = None
    cost_source: Literal["provider", "usage", "estimated"] | None = None
    simulated: bool = False
    error: str | None = None
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    evidence_hash: str


CostState = Literal["reported", "estimated", "not_reported", "simulated", "missing"]
_PRICED_COST_STATES = frozenset({"reported", "estimated"})
JEV_INPUT_USD_PER_MTOK = 0.042
JEV_COST_ESTIMATE_NOTE = "*Estimated at $0.042/MTok input"


class CostLine(BaseModel):
    label: str
    cost_usd: float | None = None
    state: CostState

    @property
    def display(self) -> str:
        if self.state == "simulated":
            return "n/a (simulated)"
        if self.state == "missing":
            return "—"
        if self.state == "not_reported" or self.cost_usd is None:
            return "not reported"
        return format_cost_usd(self.cost_usd)

    @property
    def note(self) -> str | None:
        if self.state == "estimated":
            return JEV_COST_ESTIMATE_NOTE
        return None


class PairCostComparison(BaseModel):
    jev: CostLine
    llm: CostLine
    delta_usd: float | None = None

    @property
    def comparable(self) -> bool:
        return self.jev.state in _PRICED_COST_STATES and self.llm.state in _PRICED_COST_STATES


class JudgmentAgreement(BaseModel):
    material: bool
    promotion_related: bool
    primary_driver: bool
    needs_human_review: bool
    action: bool
    agreements: int
    total: int

    @property
    def summary(self) -> str:
        return f"{self.agreements} / {self.total} decisions agree"


class JudgmentExperiment(BaseModel):
    evidence_hash: str
    runs: list[JudgmentRun]


class FieldDistribution(BaseModel):
    field: str
    counts: dict[str, int]
    mode: str | None = None
    mode_count: int = 0
    consistency: float | None = None
    n: int = 0


class ProviderAvailability(BaseModel):
    pydantic_ai: bool
    jev_key: bool
    jev_model: str
    llm_model: str | None
    llm_key_env: str | None
    llm_key: bool

    @property
    def jev_ready(self) -> bool:
        return self.pydantic_ai and self.jev_key

    @property
    def llm_ready(self) -> bool:
        return self.pydantic_ai and bool(self.llm_model) and self.llm_key


class JudgmentProvider(Protocol):
    name: str
    model: str

    def judge(self, evidence_text: str, *, evidence_hash: str) -> JudgmentRun: ...


def compare_judgments(left: BusinessJudgment, right: BusinessJudgment) -> JudgmentAgreement:
    flags = {field: getattr(left, field) == getattr(right, field) for field in JUDGMENT_FIELDS}
    agreements = sum(1 for value in flags.values() if value)
    return JudgmentAgreement(
        material=flags["material"],
        promotion_related=flags["promotion_related"],
        primary_driver=flags["primary_driver"],
        needs_human_review=flags["needs_human_review"],
        action=flags["action"],
        agreements=agreements,
        total=len(JUDGMENT_FIELDS),
    )


def field_distribution(runs: list[JudgmentRun], field: str) -> FieldDistribution:
    values = [str(getattr(run.judgment, field)) for run in runs if run.judgment is not None]
    counts = dict(Counter(values))
    n = len(values)
    if not values:
        return FieldDistribution(field=field, counts={}, n=0)
    mode, mode_count = max(counts.items(), key=lambda item: (item[1], item[0]))
    return FieldDistribution(
        field=field,
        counts=counts,
        mode=mode,
        mode_count=mode_count,
        consistency=mode_count / n,
        n=n,
    )


def experiment_consistency(
    experiment: JudgmentExperiment, provider: str
) -> dict[str, FieldDistribution]:
    runs = [run for run in experiment.runs if run.provider == provider and run.judgment is not None]
    return {field: field_distribution(runs, field) for field in JUDGMENT_FIELDS}


def latency_stats(runs: list[JudgmentRun]) -> dict[str, float] | None:
    values = [run.latency_ms for run in runs if run.latency_ms is not None]
    if not values:
        return None
    return {
        "n": float(len(values)),
        "min": min(values),
        "max": max(values),
        "median": float(median(values)),
    }


def _as_usd(value: Any) -> float | None:
    """Parse a provider/pydantic-ai cost as USD. Unknown stays None, never 0."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = float(value)
    elif isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(Decimal(value.strip()))
        except (InvalidOperation, ValueError, AttributeError):
            return None
    else:
        return None
    if not isfinite(number):
        return None
    return number


def format_cost_usd(value: float | None) -> str:
    if value is None:
        return "not reported"
    if value == 0:
        return "$0"
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude < 0.0001:
        body = f"{magnitude:.8f}".rstrip("0").rstrip(".")
    elif magnitude < 0.01:
        body = f"{magnitude:.6f}".rstrip("0").rstrip(".")
    else:
        body = f"{magnitude:,.4f}"
    return f"{sign}${body}"


def extract_cost_usd(
    *,
    usage: dict[str, Any] | None = None,
    provider_details: dict[str, Any] | None = None,
    simulated: bool = False,
) -> float | None:
    """Best-effort USD from pydantic-ai usage or a provider-reported cost.

    Simulated fixture runs never receive a monetary cost. Missing cost stays
    ``None`` so it is not treated as $0.
    """
    if simulated:
        return None
    if usage:
        parsed = _as_usd(usage.get("cost"))
        if parsed is not None:
            return parsed
    if provider_details:
        for key in ("cost", "upstream_inference_cost"):
            parsed = _as_usd(provider_details.get(key))
            if parsed is not None:
                return parsed
    return None


def usage_input_tokens(usage: dict[str, Any] | None) -> int | None:
    """Return billed input tokens from usage, or None if they were not reported."""
    if not usage:
        return None
    for key in ("input_tokens", "request_tokens"):
        raw = usage.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        if isinstance(raw, int) and raw >= 0:
            return raw
        if isinstance(raw, float) and isfinite(raw) and raw >= 0:
            return int(raw)
        if isinstance(raw, str):
            try:
                number = int(raw.strip())
            except ValueError:
                continue
            if number >= 0:
                return number
    return None


def estimate_jev_cost_usd(usage: dict[str, Any] | None) -> float | None:
    """Jev spend from actual input tokens at $0.042 per million.

    Does not invent tokens. Returns None when input usage is missing.
    """
    tokens = usage_input_tokens(usage)
    if tokens is None:
        return None
    return tokens * JEV_INPUT_USD_PER_MTOK / 1_000_000


def assign_cost(
    *,
    provider: str,
    usage: dict[str, Any] | None = None,
    provider_details: dict[str, Any] | None = None,
    simulated: bool = False,
) -> tuple[float | None, Literal["provider", "usage", "estimated"] | None]:
    if simulated:
        return None, None
    reported = extract_cost_usd(usage=usage, provider_details=provider_details)
    if reported is not None:
        source: Literal["provider", "usage"] = (
            "usage" if usage and usage.get("cost") is not None else "provider"
        )
        return reported, source
    if provider == "jev":
        estimated = estimate_jev_cost_usd(usage)
        if estimated is not None:
            return estimated, "estimated"
    return None, None


def resolve_run_cost(run: JudgmentRun) -> tuple[float | None, CostState]:
    if run.simulated:
        return None, "simulated"
    if run.cost_source == "estimated":
        amount = run.cost_usd if run.cost_usd is not None else estimate_jev_cost_usd(run.usage)
        if amount is None:
            return None, "not_reported"
        return amount, "estimated"
    reported = run.cost_usd
    if reported is None:
        reported = extract_cost_usd(usage=run.usage, provider_details=run.provider_details)
    if reported is not None:
        return reported, "reported"
    if run.provider == "jev":
        estimated = estimate_jev_cost_usd(run.usage)
        if estimated is not None:
            return estimated, "estimated"
    return None, "not_reported"


def run_cost_usd(run: JudgmentRun) -> float | None:
    amount, _state = resolve_run_cost(run)
    return amount


def _cost_line(run: JudgmentRun | None, label: str) -> CostLine:
    if run is None:
        return CostLine(label=label, state="missing")
    amount, state = resolve_run_cost(run)
    return CostLine(label=label, cost_usd=amount, state=state)


def compare_costs(jev: JudgmentRun | None, llm: JudgmentRun | None) -> PairCostComparison:
    left = _cost_line(jev, "Jev")
    right = _cost_line(llm, "Classic LLM")
    delta = None
    if left.state in _PRICED_COST_STATES and right.state in _PRICED_COST_STATES:
        assert left.cost_usd is not None and right.cost_usd is not None
        delta = right.cost_usd - left.cost_usd
    return PairCostComparison(jev=left, llm=right, delta_usd=delta)


def cost_stats(runs: list[JudgmentRun]) -> dict[str, float] | None:
    values = [amount for run in runs if (amount := run_cost_usd(run)) is not None]
    if not values:
        return None
    return {
        "n": float(len(values)),
        "n_runs": float(len(runs)),
        "min": min(values),
        "max": max(values),
        "median": float(median(values)),
        "total": float(sum(values)),
    }


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


def _provider_prefix(model: str) -> str:
    if ":" not in model:
        return model
    return model.split(":", 1)[0]


def llm_key_env_for(model: str) -> str | None:
    prefix = _provider_prefix(model)
    mapping = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google-gla": "GOOGLE_API_KEY",
        "google-vertex": "GOOGLE_API_KEY",
        "gemini": "GOOGLE_API_KEY",
        "groq": "GROQ_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "cohere": "COHERE_API_KEY",
        "bedrock": "AWS_ACCESS_KEY_ID",
        "azure": "AZURE_OPENAI_API_KEY",
        "azure-openai": "AZURE_OPENAI_API_KEY",
    }
    return mapping.get(prefix)


def configured_models() -> tuple[str, str | None]:
    jev_model = os.environ.get("JEV_MODEL", DEFAULT_JEV_MODEL).strip() or DEFAULT_JEV_MODEL
    llm_raw = os.environ.get("DEMO_LLM_MODEL", "").strip()
    return jev_model, llm_raw or None


def detect_availability() -> ProviderAvailability:
    installed = False
    try:
        import pydantic_ai  # noqa: F401

        installed = True
    except ImportError:
        installed = False
    jev_model, llm_model = configured_models()
    llm_env = llm_key_env_for(llm_model) if llm_model else None
    return ProviderAvailability(
        pydantic_ai=installed,
        jev_key=bool(os.environ.get("TYPESAFE_API_KEY")),
        jev_model=jev_model,
        llm_model=llm_model,
        llm_key_env=llm_env,
        llm_key=bool(llm_env and os.environ.get(llm_env)),
    )


def _jsonable_usage_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
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
            key: _jsonable_usage_value(value)
            for key, value in dumped.items()
            if value not in (None, {}, [])
        }
    if isinstance(raw, dict):
        return {
            key: _jsonable_usage_value(value)
            for key, value in raw.items()
            if value not in (None, {}, [])
        }
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
                out[key] = _jsonable_usage_value(value)
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


def _run_agent(model: str, evidence_text: str, *, provider: str, evidence_hash: str) -> JudgmentRun:
    from pydantic_ai import Agent

    agent = Agent(model, output_type=BusinessJudgment, instructions=SHARED_INSTRUCTIONS)
    started = time.perf_counter()
    try:
        result = _call_sync(lambda: agent.run_sync(evidence_text))
    except Exception as exc:  # noqa: BLE001 — surface provider failures in the demo
        latency = (time.perf_counter() - started) * 1000.0
        return JudgmentRun(
            provider=provider,
            model=model,
            latency_ms=round(latency, 1),
            error=redact_secrets(f"{type(exc).__name__}: {exc}"),
            evidence_hash=evidence_hash,
        )
    latency = (time.perf_counter() - started) * 1000.0
    output = getattr(result, "output", None)
    judgment = None
    error = None
    if isinstance(output, BusinessJudgment):
        judgment = output
    else:
        try:
            judgment = BusinessJudgment.model_validate(output)
        except Exception as exc:  # noqa: BLE001
            error = redact_secrets(f"schema validation failed: {exc}")
    usage = _usage_dict(result)
    details = _provider_details(result)
    cost_usd, cost_source = assign_cost(provider=provider, usage=usage, provider_details=details)
    return JudgmentRun(
        provider=provider,
        model=_result_model_name(result, model),
        judgment=judgment,
        latency_ms=round(latency, 1),
        usage=usage,
        provider_details=details,
        cost_usd=cost_usd,
        cost_source=cost_source,
        error=error,
        evidence_hash=evidence_hash,
    )


class LiveJevProvider:
    name = "jev"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or configured_models()[0]

    def judge(self, evidence_text: str, *, evidence_hash: str) -> JudgmentRun:
        if not os.environ.get("TYPESAFE_API_KEY"):
            return JudgmentRun(
                provider=self.name,
                model=self.model,
                error="TYPESAFE_API_KEY is not configured",
                evidence_hash=evidence_hash,
            )
        return _run_agent(
            self.model, evidence_text, provider=self.name, evidence_hash=evidence_hash
        )


class LiveLLMProvider:
    name = "llm"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or configured_models()[1] or ""

    def judge(self, evidence_text: str, *, evidence_hash: str) -> JudgmentRun:
        if not self.model:
            return JudgmentRun(
                provider=self.name,
                model="",
                error="DEMO_LLM_MODEL is not configured",
                evidence_hash=evidence_hash,
            )
        env_name = llm_key_env_for(self.model)
        if env_name and not os.environ.get(env_name):
            return JudgmentRun(
                provider=self.name,
                model=self.model,
                error=f"{env_name} is not configured",
                evidence_hash=evidence_hash,
            )
        return _run_agent(
            self.model, evidence_text, provider=self.name, evidence_hash=evidence_hash
        )


def _judgment(**kwargs: Any) -> BusinessJudgment:
    return BusinessJudgment.model_validate(kwargs)


# Simulated outputs for fixture mode. Differences are authored for the demo
# and do not claim measured provider behaviour.
_JEV_FIXTURES: dict[str, BusinessJudgment] = {
    "great_lunch": _judgment(
        material=True,
        promotion_related=True,
        primary_driver=BusinessDriver.promotion_behavior,
        needs_human_review=True,
        action=RecommendedAction.review_promotion,
    ),
    "healthy_growth": _judgment(
        material=False,
        promotion_related=False,
        primary_driver=BusinessDriver.volume,
        needs_human_review=False,
        action=RecommendedAction.observe,
    ),
    "cost_pressure": _judgment(
        material=True,
        promotion_related=False,
        primary_driver=BusinessDriver.cost_pressure,
        needs_human_review=True,
        action=RecommendedAction.investigate_costs,
    ),
    "ambiguous": _judgment(
        material=True,
        promotion_related=True,
        primary_driver=BusinessDriver.unclear,
        needs_human_review=True,
        action=RecommendedAction.human_review,
    ),
}

_LLM_FIXTURES: dict[str, BusinessJudgment] = {
    "great_lunch": _judgment(
        material=True,
        promotion_related=True,
        primary_driver=BusinessDriver.product_mix,
        needs_human_review=True,
        action=RecommendedAction.review_promotion,
    ),
    "healthy_growth": _judgment(
        material=False,
        promotion_related=False,
        primary_driver=BusinessDriver.volume,
        needs_human_review=False,
        action=RecommendedAction.observe,
    ),
    "cost_pressure": _judgment(
        material=True,
        promotion_related=False,
        primary_driver=BusinessDriver.cost_pressure,
        needs_human_review=True,
        action=RecommendedAction.investigate_costs,
    ),
    "ambiguous": _judgment(
        material=True,
        promotion_related=False,
        primary_driver=BusinessDriver.product_mix,
        needs_human_review=True,
        action=RecommendedAction.human_review,
    ),
}

_JEV_FIXTURE_DETAILS: dict[str, dict[str, Any]] = {
    "great_lunch": {
        "confidence": {
            "material": 0.91,
            "promotion_related": 0.86,
            "primary_driver": 0.78,
            "needs_human_review": 0.88,
            "action": 0.81,
        },
        "probabilities": {
            "primary_driver": {
                "promotion_behavior": 0.78,
                "cost_pressure": 0.14,
                "product_mix": 0.06,
                "unclear": 0.02,
            },
            "action": {
                "review_promotion": 0.81,
                "human_review": 0.12,
                "investigate_costs": 0.05,
                "observe": 0.02,
            },
        },
    },
    "healthy_growth": {
        "confidence": {
            "material": 0.84,
            "promotion_related": 0.90,
            "primary_driver": 0.72,
            "needs_human_review": 0.80,
            "action": 0.83,
        },
        "probabilities": {
            "primary_driver": {
                "volume": 0.72,
                "pricing": 0.12,
                "unclear": 0.10,
                "promotion_behavior": 0.06,
            }
        },
    },
    "cost_pressure": {
        "confidence": {
            "material": 0.88,
            "promotion_related": 0.82,
            "primary_driver": 0.80,
            "needs_human_review": 0.77,
            "action": 0.79,
        },
        "probabilities": {
            "primary_driver": {
                "cost_pressure": 0.80,
                "pricing": 0.09,
                "unclear": 0.07,
                "promotion_behavior": 0.04,
            }
        },
    },
    "ambiguous": {
        "confidence": {
            "material": 0.64,
            "promotion_related": 0.52,
            "primary_driver": 0.41,
            "needs_human_review": 0.86,
            "action": 0.74,
        },
        "probabilities": {
            "primary_driver": {
                "unclear": 0.41,
                "promotion_behavior": 0.27,
                "product_mix": 0.18,
                "cost_pressure": 0.14,
            }
        },
    },
}


class FixtureJudgmentProvider:
    def __init__(self, provider: ProviderName, scenario_id: str) -> None:
        self.name = provider
        self.scenario_id = scenario_id
        self.model = "fixture:jev" if provider == "jev" else "fixture:llm"

    def judge(self, evidence_text: str, *, evidence_hash: str) -> JudgmentRun:
        del evidence_text
        table = _JEV_FIXTURES if self.name == "jev" else _LLM_FIXTURES
        if self.scenario_id not in table:
            return JudgmentRun(
                provider=self.name,
                model=self.model,
                simulated=True,
                error=f"No fixture for scenario {self.scenario_id!r}",
                evidence_hash=evidence_hash,
            )
        details = _JEV_FIXTURE_DETAILS.get(self.scenario_id) if self.name == "jev" else None
        return JudgmentRun(
            provider=self.name,
            model=self.model,
            judgment=table[self.scenario_id],
            simulated=True,
            provider_details=details,
            evidence_hash=evidence_hash,
        )


def fixture_run(provider: ProviderName, scenario_id: str, evidence_hash: str) -> JudgmentRun:
    return FixtureJudgmentProvider(provider, scenario_id).judge("", evidence_hash=evidence_hash)


def jev_confidence(run: JudgmentRun) -> dict[str, float]:
    details = run.provider_details or {}
    raw = details.get("confidence")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def jev_probabilities(run: JudgmentRun, field: str) -> dict[str, float]:
    details = run.provider_details or {}
    raw = details.get("probabilities")
    if not isinstance(raw, dict):
        return {}
    item = raw.get(field)
    if not isinstance(item, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in item.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def relevant_jev_confidence(run: JudgmentRun) -> float | None:
    """Minimum available Jev field confidence, if the provider returned any."""
    values = list(jev_confidence(run).values())
    if not values:
        return None
    return min(values)


def run_experiment(
    *,
    jev: JudgmentProvider,
    llm: JudgmentProvider,
    evidence_text: str,
    evidence_hash: str,
    n: int = 5,
    runner: Callable[[JudgmentProvider, str, str], JudgmentRun] | None = None,
) -> JudgmentExperiment:
    if n < 1 or n > 5:
        raise ValueError("repeated-run experiment is capped at 5")
    execute = runner or (lambda provider, text, digest: provider.judge(text, evidence_hash=digest))
    runs: list[JudgmentRun] = []
    for _ in range(n):
        runs.append(execute(jev, evidence_text, evidence_hash))
        runs.append(execute(llm, evidence_text, evidence_hash))
    return JudgmentExperiment(evidence_hash=evidence_hash, runs=runs)
