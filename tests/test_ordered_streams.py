"""Ordered metric-state stream processing tests."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from metric_runtime import (
    KPI,
    Formula,
    InMemoryStateStore,
    KPIEngine,
    KPIState,
    StatePolicy,
)
from metric_runtime.exceptions import StaleEvaluationError
from metric_runtime.identity import EvaluationKey, canonical_scope_key
from metric_runtime.models import (
    Directionality,
    ExplanatoryCandidate,
    InvestigationResult,
    KPIStatus,
)
from metric_runtime.notifications import RecordingNotifier


def _ts(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _status(name: str, at: datetime, *, anomaly: bool) -> KPIStatus:
    return KPIStatus(
        name=name,
        value=1.0,
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


def _engine() -> KPIEngine:
    catalog = [
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
    return KPIEngine(
        catalog=catalog,
        state_store=InMemoryStateStore(),
        notifier=RecordingNotifier(),
        state_policy=StatePolicy(
            persistence=2, min_impact_eur=1.0, resolve_after_healthy_windows=2
        ),
    )


def _patch_anomaly(monkeypatch, engine: KPIEngine) -> dict[str, int]:
    calls = {"evaluate": 0}
    lock = threading.Lock()

    def fake_evaluate(name, at, filters=None):
        with lock:
            calls["evaluate"] += 1
        return _status(name, at, anomaly=True)

    def fake_investigate(metric, at=None, **kwargs):
        status = _status("basket_cliff", at or _ts(2026, 5, 15, 12, 30), anomaly=True)
        primary = ExplanatoryCandidate(
            name="basket_cliff",
            owner="commercial-growth",
            depth=1,
            status=status,
            impact_eur=120.0,
            score=50.0,
        )
        return InvestigationResult(
            metric=metric,
            anomalous_metrics=[_status(metric, at or status.as_of, anomaly=True), status],
            normal_dependencies=[],
            root_candidates=[status],
            explanatory_paths=[[metric, "basket_cliff"]],
            deepest_candidates=[primary],
            primary_explanatory=primary,
            impact_eur=120.0,
        )

    monkeypatch.setattr(engine, "evaluate", fake_evaluate)
    monkeypatch.setattr(engine, "estimate_impact_eur", lambda *a, **k: 120.0)
    monkeypatch.setattr(engine, "investigate", fake_investigate)
    return calls


def test_concurrent_windows_apply_in_effective_at_order(monkeypatch):
    """12:00 and 12:30 start together; final state matches sequential order.

    Regardless of which warehouse evaluation finishes first, commits must be:
      12:00 NORMAL → DETECTED
      12:30 DETECTED → OPEN
    """
    engine = _engine()
    calls = _patch_anomaly(monkeypatch, engine)
    scope = {"city": "Amsterdam"}
    t12 = _ts(2026, 5, 15, 12, 0)
    t1230 = _ts(2026, 5, 15, 12, 30)

    # Both workers claim before either may finish warehouse work.
    claimed = threading.Barrier(3)
    original_evaluate = engine.evaluate

    def gated_evaluate(name, at, filters=None):
        claimed.wait(timeout=5)
        return original_evaluate(name, at, filters)

    monkeypatch.setattr(engine, "evaluate", gated_evaluate)

    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def worker(label: str, at: datetime) -> None:
        try:
            results[label] = engine.process(metric="profit_margin", at=at, scope=scope)
        except BaseException as exc:  # noqa: BLE001
            errors[label] = exc

    threads = [
        threading.Thread(target=worker, args=("a", t12)),
        threading.Thread(target=worker, args=("b", t1230)),
    ]
    for thread in threads:
        thread.start()
    claimed.wait(timeout=5)  # release both evaluates together
    for thread in threads:
        thread.join(timeout=15)

    assert errors == {}, errors
    a = results["a"]
    b = results["b"]
    assert a.transition == (KPIState.NORMAL, KPIState.DETECTED)
    assert b.transition == (KPIState.DETECTED, KPIState.OPEN)
    assert a.effective_at == t12
    assert b.effective_at == t1230
    assert calls["evaluate"] == 2

    store = engine.state_store
    scope_key = canonical_scope_key(scope)
    record = store.get_state_record("profit_margin", scope_key)
    assert record.state == KPIState.OPEN
    assert record.last_evaluation_at == t1230
    assert record.version == 2
    assert len(store.list_open_incidents()) == 1
    assert len(store.get_history("profit_margin", scope_key)) == 2


def test_later_window_first_still_orders_when_earlier_is_inflight(monkeypatch):
    """Force 12:30 warehouse to finish before 12:00; state order still correct."""
    engine = _engine()
    _patch_anomaly(monkeypatch, engine)
    scope = {"city": "Amsterdam"}
    t12 = _ts(2026, 5, 15, 12, 0)
    t1230 = _ts(2026, 5, 15, 12, 30)

    release_early = threading.Event()
    both_claimed = threading.Barrier(3)
    original_evaluate = engine.evaluate

    def ordered_evaluate(name, at, filters=None):
        both_claimed.wait(timeout=5)
        if at == t12:
            assert release_early.wait(timeout=5)
        result = original_evaluate(name, at, filters)
        if at == t1230:
            release_early.set()
        return result

    monkeypatch.setattr(engine, "evaluate", ordered_evaluate)

    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def worker(label: str, at: datetime) -> None:
        try:
            results[label] = engine.process(metric="profit_margin", at=at, scope=scope)
        except BaseException as exc:  # noqa: BLE001
            errors[label] = exc

    threads = [
        threading.Thread(target=worker, args=("a", t12)),
        threading.Thread(target=worker, args=("b", t1230)),
    ]
    for thread in threads:
        thread.start()
    both_claimed.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=15)

    assert errors == {}, errors
    assert results["a"].transition == (KPIState.NORMAL, KPIState.DETECTED)
    assert results["b"].transition == (KPIState.DETECTED, KPIState.OPEN)


def test_stale_older_window_rejected_after_newer_commit(monkeypatch):
    engine = _engine()
    _patch_anomaly(monkeypatch, engine)
    scope = {"city": "Amsterdam"}
    t12 = _ts(2026, 5, 15, 12, 0)
    t1230 = _ts(2026, 5, 15, 12, 30)

    first = engine.process(metric="profit_margin", at=t1230, scope=scope)
    assert first.transition == (KPIState.NORMAL, KPIState.DETECTED)

    with pytest.raises(StaleEvaluationError):
        engine.process(metric="profit_margin", at=t12, scope=scope)

    record = engine.state_store.get_state_record("profit_margin", canonical_scope_key(scope))
    assert record.last_evaluation_at == t1230
    assert record.state == KPIState.DETECTED


def test_independent_scopes_process_concurrently(monkeypatch):
    engine = _engine()
    _patch_anomaly(monkeypatch, engine)
    t = _ts(2026, 5, 15, 12, 0)
    ams = {"city": "Amsterdam"}
    rdam = {"city": "Rotterdam"}

    entered = threading.Barrier(3)
    release = threading.Event()
    original_evaluate = engine.evaluate
    in_eval = {"n": 0}
    lock = threading.Lock()

    def blocked_evaluate(name, at, filters=None):
        with lock:
            in_eval["n"] += 1
            current = in_eval["n"]
        entered.wait(timeout=5)
        # Both scopes must be inside evaluate concurrently before either proceeds.
        assert current >= 1
        assert release.wait(timeout=5)
        return original_evaluate(name, at, filters)

    monkeypatch.setattr(engine, "evaluate", blocked_evaluate)

    results: list[object] = []
    errors: list[BaseException] = []

    def worker(scope: dict[str, str]) -> None:
        try:
            results.append(engine.process(metric="profit_margin", at=t, scope=scope))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(ams,)),
        threading.Thread(target=worker, args=(rdam,)),
    ]
    for thread in threads:
        thread.start()
    entered.wait(timeout=5)
    assert in_eval["n"] == 2
    release.set()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert {r.scope["city"] for r in results} == {"Amsterdam", "Rotterdam"}
    assert all(r.transition == (KPIState.NORMAL, KPIState.DETECTED) for r in results)


def test_suppress_without_incident_does_not_enqueue_outbox(monkeypatch):
    engine = _engine()
    monkeypatch.setattr(
        engine,
        "evaluate",
        lambda name, at, filters=None: _status(name, at, anomaly=True),
    )
    monkeypatch.setattr(engine, "estimate_impact_eur", lambda *a, **k: 120.0)
    from metric_runtime.models import QualityReport

    bad = QualityReport(
        healthy=False,
        freshness_ok=False,
        completeness_ok=True,
        volume_ok=True,
        row_count=0,
        missing_rate=0.0,
        latest_ts="",
        message="stale",
    )
    result = engine.process(
        metric="profit_margin",
        at=_ts(2026, 5, 15, 12, 0),
        scope={"city": "Amsterdam"},
        quality=bad,
    )
    assert result.transition == (KPIState.NORMAL, KPIState.SUPPRESSED)
    assert result.notifications == []
    assert engine.state_store.list_pending_notifications() == []


def test_evaluation_record_tracks_effective_and_processed_at(monkeypatch):
    engine = _engine()
    _patch_anomaly(monkeypatch, engine)
    at = _ts(2026, 5, 15, 12, 0)
    result = engine.process(metric="profit_margin", at=at, scope={"city": "Amsterdam"})
    assert result.effective_at == at
    assert result.processed_at is not None
    assert result.evaluation_record is not None
    assert result.evaluation_record.effective_at == at
    assert result.evaluation_record.processed_at == result.processed_at
    key = EvaluationKey.build("profit_margin", scope={"city": "Amsterdam"}, at=at)
    stored = engine.state_store.get_committed_result(key)
    assert stored is not None
    assert stored.state_record.last_evaluation_at == at
    assert stored.state_record.version == 1
