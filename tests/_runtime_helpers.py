"""Helpers shared by runtime-store / scheduler tests."""

from __future__ import annotations

from datetime import UTC, datetime

from metric_runtime import KPI, Formula, KPIEngine, KPIState, StatePolicy
from metric_runtime.models import (
    Directionality,
    ExplanatoryCandidate,
    InvestigationResult,
    KPIStatus,
)
from metric_runtime.notifications import RecordingNotifier


def ts(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def status(name: str, at: datetime, *, anomaly: bool, value: float = 1.0) -> KPIStatus:
    return KPIStatus(
        name=name,
        value=value,
        baseline_mean=2.0,
        baseline_std=0.1,
        z_score=-5.0 if anomaly else 0.2,
        relative_change=-0.5 if anomaly else 0.01,
        anomaly=anomaly,
        support=100,
        support_ok=True,
        as_of=at,
        directionality=Directionality.LOWER_IS_BAD,
        state=KPIState.DETECTED if anomaly else KPIState.NORMAL,
        severity=8.0 if anomaly else 0.0,
    )


def catalog() -> list[KPI]:
    return [
        KPI(
            name="basket_cliff",
            owner="commercial-growth",
            formula=Formula.sum("basket_cliff"),
            directionality=Directionality.HIGHER_IS_BAD,
        ),
        KPI(
            name="profit_margin",
            owner="finance",
            formula=Formula.ratio("profit", "revenue"),
            dependencies=("basket_cliff",),
            directionality=Directionality.LOWER_IS_BAD,
        ),
    ]


def engine(store, *, notifier=None) -> KPIEngine:
    return KPIEngine(
        catalog=catalog(),
        runtime_store=store,
        notifier=notifier or RecordingNotifier(),
        state_policy=StatePolicy(
            persistence=2, min_impact_eur=1.0, resolve_after_healthy_windows=2
        ),
    )


def fake_investigation(metric: str, at: datetime) -> InvestigationResult:
    st = status("basket_cliff", at, anomaly=True)
    primary = ExplanatoryCandidate(
        name="basket_cliff",
        owner="commercial-growth",
        depth=1,
        status=st,
        impact_eur=120.0,
        score=50.0,
    )
    return InvestigationResult(
        metric=metric,
        anomalous_metrics=[status(metric, at, anomaly=True), st],
        normal_dependencies=[],
        root_candidates=[st],
        explanatory_paths=[[metric, "basket_cliff"]],
        deepest_candidates=[primary],
        primary_explanatory=primary,
        impact_eur=120.0,
    )


def patch_anomaly(monkeypatch, eng: KPIEngine, *, anomaly: bool = True) -> dict[str, int]:
    """Make evaluate() return a fixed anomaly status; count calls."""
    calls = {"evaluate": 0}

    def fake_evaluate(name, at, filters=None, **kwargs):
        calls["evaluate"] += 1
        return status(name, at, anomaly=anomaly)

    def fake_investigate(metric, at=None, **kwargs):
        return fake_investigation(metric, at or ts(2026, 5, 15, 12, 0))

    monkeypatch.setattr(eng, "evaluate", fake_evaluate)
    monkeypatch.setattr(eng, "estimate_impact_eur", lambda *a, **k: 120.0)
    monkeypatch.setattr(eng, "investigate", fake_investigate)
    return calls


def patch_script(
    monkeypatch, eng: KPIEngine, script, *, default: str = "healthy"
) -> list[tuple[str, datetime]]:
    """Drive evaluate() from ``script(name, at) -> outcome`` (or a dict keyed by ``at``).

    Outcomes: ``anomaly``, ``healthy``, ``no_data``, ``error``. Returns the
    list of ``(metric, at)`` calls in order.
    """
    from metric_runtime.exceptions import MetricRuntimeError, NoDataError

    calls: list[tuple[str, datetime]] = []

    def outcome_for(name: str, at: datetime) -> str:
        if callable(script):
            return script(name, at)
        return script.get(at, default)

    def fake_evaluate(name, at, filters=None, **kwargs):
        calls.append((name, at))
        outcome = outcome_for(name, at)
        if outcome == "no_data":
            raise NoDataError(f"{name} has no data at {at.isoformat()}")
        if outcome == "error":
            raise MetricRuntimeError(f"{name} failed at {at.isoformat()}")
        return status(name, at, anomaly=outcome == "anomaly")

    def fake_investigate(metric, at=None, **kwargs):
        return fake_investigation(metric, at or ts(2026, 5, 15, 12, 0))

    monkeypatch.setattr(eng, "evaluate", fake_evaluate)
    monkeypatch.setattr(eng, "estimate_impact_eur", lambda *a, **k: 120.0)
    monkeypatch.setattr(eng, "investigate", fake_investigate)
    return calls
