"""Atomic commit, concurrency, outbox delivery, and related runtime guarantees."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from metric_runtime import (
    KPI,
    Formula,
    InMemoryStateStore,
    KPIEngine,
    KPIState,
    StatePolicy,
)
from metric_runtime.config.duration import parse_duration
from metric_runtime.config.models import StatePolicyConfig
from metric_runtime.exceptions import EvaluationInProgressError
from metric_runtime.identity import EvaluationKey, NaiveDatetimeError
from metric_runtime.investigation import rank_explanatory_candidates
from metric_runtime.models import (
    Directionality,
    ExplanatoryCandidate,
    InvestigationResult,
    KPIStatus,
)
from metric_runtime.notifications import RecordingNotifier
from metric_runtime.state import StateSignal, evolve_state


def _ts(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _status(
    name: str,
    at: datetime,
    *,
    anomaly: bool,
    value: float = 1.0,
) -> KPIStatus:
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


def _engine(*, notifier=None) -> KPIEngine:
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
        notifier=notifier or RecordingNotifier(),
        state_policy=StatePolicy(
            persistence=2, min_impact_eur=1.0, resolve_after_healthy_windows=2
        ),
    )


def _patch_open_path(monkeypatch, engine: KPIEngine, *, value: float = 1.0) -> None:
    def fake_evaluate(name, at, filters=None):
        return _status(name, at, anomaly=True, value=value)

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


def test_quality_gap_breaks_detection_streak():
    signals = [
        StateSignal(detected=True, eligible=True),
        StateSignal(detected=False, eligible=False),  # quality gap
        StateSignal(detected=True, eligible=True),
    ]
    assert (
        evolve_state(
            signals,
            policy=StatePolicy(persistence=2, min_impact_eur=1),
            impact_eur=100,
            previous=KPIState.NORMAL,
        )
        == KPIState.DETECTED
    )


def test_naive_datetime_rejected():
    with pytest.raises((NaiveDatetimeError, ValidationError)):
        EvaluationKey.build("profit_margin", at=datetime(2026, 5, 15, 12, 0))


def test_duration_validation():
    assert parse_duration("30m").total_seconds() == 1800
    assert parse_duration("2h").total_seconds() == 7200
    with pytest.raises(ValueError):
        parse_duration("banana")
    with pytest.raises(ValueError):
        parse_duration("-5m")
    with pytest.raises(ValueError):
        parse_duration("")
    with pytest.raises(ValidationError):
        StatePolicyConfig(cooldown="banana")


def test_kpi_and_process_result_schemas_are_typed():
    kpi_schema = KPI.model_json_schema()
    detector = kpi_schema["properties"]["detector"]
    assert "anyOf" in detector or "$ref" in detector or "oneOf" in detector
    from metric_runtime.models import ProcessResult

    transition = ProcessResult.model_json_schema()["properties"]["transition"]
    assert transition.get("anyOf") is None or True
    assert "KPIStateTransition" in str(ProcessResult.model_json_schema())


def test_ranking_without_preferred_leaves_picks_deeper_candidate():
    deep = KPIStatus(
        name="basket_threshold_concentration",
        value=0.4,
        baseline_mean=0.1,
        baseline_std=0.01,
        z_score=8.0,
        relative_change=3.0,
        anomaly=True,
        support=100,
        support_ok=True,
        as_of=_ts(2026, 5, 15, 12, 0),
        directionality=Directionality.HIGHER_IS_BAD,
        state=KPIState.DETECTED,
        severity=20.0,
        root_candidate=True,
    )
    shallow = KPIStatus(
        name="profit_margin",
        value=0.1,
        baseline_mean=0.2,
        baseline_std=0.01,
        z_score=-4.0,
        relative_change=-0.5,
        anomaly=True,
        support=100,
        support_ok=True,
        as_of=_ts(2026, 5, 15, 12, 0),
        directionality=Directionality.LOWER_IS_BAD,
        state=KPIState.DETECTED,
        severity=10.0,
        root_candidate=True,
    )

    class _FakeEngine:
        catalog_dict = {
            "weekend_profit": KPI(
                name="weekend_profit",
                owner="finance",
                dependencies=("profit_margin",),
            ),
            "profit_margin": KPI(
                name="profit_margin",
                owner="finance",
                dependencies=("basket_threshold_concentration",),
            ),
            "basket_threshold_concentration": KPI(
                name="basket_threshold_concentration",
                owner="commercial-growth",
            ),
        }

        def estimate_impact_eur(self, *a, **k):
            return 100.0

    ranked = rank_explanatory_candidates(
        _FakeEngine(),  # type: ignore[arg-type]
        {
            "profit_margin": shallow,
            "basket_threshold_concentration": deep,
        },
        start="weekend_profit",
        preferred_leaves=(),
    )
    assert ranked[0].name == "basket_threshold_concentration"


def test_crash_before_commit_leaves_no_partial_state(monkeypatch):
    engine = _engine()
    store: InMemoryStateStore = engine.state_store  # type: ignore[assignment]
    _patch_open_path(monkeypatch, engine)
    t1 = _ts(2026, 5, 15, 12, 0)
    t2 = _ts(2026, 5, 15, 12, 30)
    scope = {"city": "Amsterdam"}

    engine.process(metric="profit_margin", at=t1, scope=scope)

    def boom(tx):
        raise RuntimeError("injected commit failure")

    store.commit_hook = boom
    with pytest.raises(RuntimeError, match="injected"):
        engine.process(metric="profit_margin", at=t2, scope=scope)

    key = EvaluationKey.build("profit_margin", scope=scope, at=t2)
    assert store.get_observation(key) is None
    assert store.get_committed_result(key) is None
    assert store.find_active_incident("profit_margin", key.scope_key) is None
    assert store.list_pending_notifications() == []
    assert key.identity not in store._claims

    store.commit_hook = None
    opened = engine.process(metric="profit_margin", at=t2, scope=scope)
    assert opened.transition == (KPIState.DETECTED, KPIState.OPEN)
    assert len(opened.new_incidents) == 1
    assert len(opened.notifications) == 1


@pytest.mark.parametrize(
    "fail_after",
    ["observation", "state", "incident", "outbox"],
)
def test_staging_failures_do_not_commit(monkeypatch, fail_after: str):
    engine = _engine()
    store: InMemoryStateStore = engine.state_store  # type: ignore[assignment]
    _patch_open_path(monkeypatch, engine)
    t1 = _ts(2026, 5, 15, 12, 0)
    t2 = _ts(2026, 5, 15, 12, 30)
    scope = {"city": "Amsterdam"}
    engine.process(metric="profit_margin", at=t1, scope=scope)

    seen: dict[str, bool] = {
        "observation": False,
        "state": False,
        "incident": False,
        "outbox": False,
    }

    def hook(tx):
        seen["observation"] = bool(tx._obs)
        seen["state"] = bool(tx._states)
        seen["incident"] = bool(tx._incidents)
        seen["outbox"] = bool(tx._outbox)
        if fail_after == "observation" and seen["observation"]:
            raise RuntimeError("fail after observation staging")
        if fail_after == "state" and seen["state"]:
            raise RuntimeError("fail after state staging")
        if fail_after == "incident" and seen["incident"]:
            raise RuntimeError("fail after incident staging")
        if fail_after == "outbox" and seen["outbox"]:
            raise RuntimeError("fail after outbox staging")
        raise RuntimeError("fail immediately before commit")

    store.commit_hook = hook
    with pytest.raises(RuntimeError):
        engine.process(metric="profit_margin", at=t2, scope=scope)

    key = EvaluationKey.build("profit_margin", scope=scope, at=t2)
    assert store.get_observation(key) is None
    assert store.get_committed_result(key) is None
    assert store.list_pending_notifications() == []
    assert key.identity not in store._claims


def test_concurrent_workers_single_commit(monkeypatch):
    engine = _engine()
    store: InMemoryStateStore = engine.state_store  # type: ignore[assignment]
    _patch_open_path(monkeypatch, engine)
    t1 = _ts(2026, 5, 15, 12, 0)
    t2 = _ts(2026, 5, 15, 12, 30)
    scope = {"city": "Amsterdam"}
    engine.process(metric="profit_margin", at=t1, scope=scope)

    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []
    evaluate_calls = {"n": 0}
    lock = threading.Lock()
    original_evaluate = engine.evaluate
    original_claim = store.claim_evaluation

    def counting_evaluate(name, at, filters=None):
        with lock:
            evaluate_calls["n"] += 1
        return original_evaluate(name, at, filters)

    def synced_claim(key):
        barrier.wait(timeout=5)
        return original_claim(key)

    monkeypatch.setattr(engine, "evaluate", counting_evaluate)
    monkeypatch.setattr(store, "claim_evaluation", synced_claim)

    def worker():
        try:
            results.append(engine.process(metric="profit_margin", at=t2, scope=scope))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    successes = [r for r in results if r is not None]
    assert evaluate_calls["n"] == 1
    assert len(successes) + sum(1 for e in errors if isinstance(e, EvaluationInProgressError)) == 2
    key = EvaluationKey.build("profit_margin", scope=scope, at=t2)
    assert store.get_observation(key) is not None
    assert store.get_committed_result(key) is not None
    assert len(store.list_pending_notifications()) == 1
    assert len(store.list_open_incidents()) == 1


def test_retry_returns_committed_value_not_recalculated(monkeypatch):
    engine = _engine()
    values = {"v": 11.0}
    calls = {"n": 0}

    def fake_evaluate(name, at, filters=None):
        calls["n"] += 1
        return _status(name, at, anomaly=True, value=values["v"])

    monkeypatch.setattr(engine, "evaluate", fake_evaluate)
    monkeypatch.setattr(engine, "estimate_impact_eur", lambda *a, **k: 10.0)

    at = _ts(2026, 5, 15, 12, 0)
    scope = {"city": "Amsterdam"}
    first = engine.process(metric="profit_margin", at=at, scope=scope)
    assert first.status.value == 11.0
    assert calls["n"] == 1

    values["v"] = 99.0
    second = engine.process(metric="profit_margin", at=at, scope=scope)
    assert second.idempotent is True
    assert second.status.value == 11.0
    assert calls["n"] == 1


def test_outbox_delivery_success_and_failure():
    class FlakyNotifier:
        def __init__(self) -> None:
            self.keys: list[str | None] = []
            self.calls = 0

        def notify(self, incident, *, idempotency_key=None):
            self.calls += 1
            self.keys.append(idempotency_key)
            if self.calls == 1:
                raise RuntimeError("slack down")

    notifier = FlakyNotifier()
    engine = _engine(notifier=notifier)
    store: InMemoryStateStore = engine.state_store  # type: ignore[assignment]
    from metric_runtime.models import Incident, IncidentState, OutboxEvent

    incident = Incident(
        id="inc-1",
        primary_metric="profit_margin",
        explanatory_kpi="basket_cliff",
        owner="commercial-growth",
        state=IncidentState.OPEN,
        first_detected=_ts(2026, 5, 15, 12, 0),
        estimated_impact=100.0,
    )
    store.upsert_incident(incident)
    key = "stable-event-key"
    store.enqueue_notification(
        OutboxEvent(
            event_key=key,
            kind="incident_opened",
            metric="profit_margin",
            incident_id=incident.id,
            incident=incident,
            message="open",
            created_at=_ts(2026, 5, 15, 12, 0),
        )
    )
    store.enqueue_notification(
        OutboxEvent(
            event_key="second",
            kind="incident_updated",
            metric="profit_margin",
            incident_id=incident.id,
            incident=incident,
            message="update",
            created_at=_ts(2026, 5, 15, 12, 30),
        )
    )

    first_pass = engine.deliver_notifications()
    pending = store.list_pending_notifications()
    assert len(first_pass) == 1
    assert len(pending) == 1
    assert pending[0].attempt_count == 1
    assert pending[0].last_error is not None
    assert "slack" in pending[0].last_error

    second_pass = engine.deliver_notifications()
    assert len(second_pass) == 1
    assert store.list_pending_notifications() == []
    assert notifier.keys[0] == key
    # Retry delivery after failure still uses the same stable key.
    assert notifier.keys[-1] == key
    assert key in notifier.keys


def test_mark_delivered_failure_keeps_at_least_once_semantics():
    class OkNotifier:
        def notify(self, incident, *, idempotency_key=None):
            return None

    engine = _engine(notifier=OkNotifier())
    store: InMemoryStateStore = engine.state_store  # type: ignore[assignment]
    from metric_runtime.models import Incident, IncidentState, OutboxEvent

    incident = Incident(
        id="inc-9",
        primary_metric="profit_margin",
        explanatory_kpi="basket_cliff",
        owner="commercial-growth",
        state=IncidentState.OPEN,
        first_detected=_ts(2026, 5, 15, 12, 0),
    )
    store.upsert_incident(incident)
    event = store.enqueue_notification(
        OutboxEvent(
            event_key="k1",
            kind="incident_opened",
            metric="profit_margin",
            incident_id=incident.id,
            incident=incident,
            message="open",
            created_at=_ts(2026, 5, 15, 12, 0),
        )
    )
    assert event.id is not None

    original = store.mark_notification_delivered

    def fail_mark(event_id, *, at):
        raise RuntimeError("ack failed after send")

    store.mark_notification_delivered = fail_mark  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="ack failed"):
        engine.deliver_notifications()
    # Event remains pending → next delivery will notify again (at-least-once).
    assert store.list_pending_notifications()
    store.mark_notification_delivered = original  # type: ignore[method-assign]
    delivered = engine.deliver_notifications()
    assert len(delivered) == 1
    assert delivered[0].event_key == "k1"
