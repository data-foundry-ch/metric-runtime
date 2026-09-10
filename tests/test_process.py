"""End-to-end runtime.process lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime

from metric_runtime import (
    KPI,
    Formula,
    InMemoryStateStore,
    KPIEngine,
    KPIState,
    StatePolicy,
)
from metric_runtime.models import (
    Directionality,
    ExplanatoryCandidate,
    InvestigationResult,
    KPIStatus,
    QualityReport,
)
from metric_runtime.notifications import RecordingNotifier


def _ts(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _status(
    name: str,
    at: datetime,
    *,
    anomaly: bool,
    value: float = 1.0,
    baseline: float = 2.0,
) -> KPIStatus:
    return KPIStatus(
        name=name,
        value=value,
        baseline_mean=baseline,
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


def _engine() -> tuple[KPIEngine, RecordingNotifier]:
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
    notifier = RecordingNotifier()
    engine = KPIEngine(
        catalog=catalog,
        state_store=InMemoryStateStore(),
        notifier=notifier,
        state_policy=StatePolicy(
            persistence=2, min_impact_eur=1.0, resolve_after_healthy_windows=2
        ),
        preferred_leaves=("basket_cliff",),
    )
    return engine, notifier


def test_process_detected_then_open_then_idempotent(monkeypatch):
    engine, notifier = _engine()
    t1 = _ts(2026, 5, 15, 12, 0)
    t2 = _ts(2026, 5, 15, 12, 30)
    scope = {"city": "Amsterdam"}
    calls = {"evaluate": 0}

    def fake_evaluate(name, at, filters=None):
        calls["evaluate"] += 1
        return _status(name, at, anomaly=True)

    def fake_investigate(metric, at=None, **kwargs):
        status = _status("basket_cliff", at or t2, anomaly=True)
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
            anomalous_metrics=[_status(metric, at or t2, anomaly=True), status],
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

    first = engine.process(metric="profit_margin", at=t1, scope=scope)
    assert first.transition == (KPIState.NORMAL, KPIState.DETECTED)
    assert first.new_incidents == []
    assert first.notifications == []
    assert notifier.incidents == []

    second = engine.process(metric="profit_margin", at=t2, scope=scope)
    assert second.transition == (KPIState.DETECTED, KPIState.OPEN)
    assert len(second.new_incidents) == 1
    assert second.new_incidents[0].owner == "commercial-growth"
    assert len(second.notifications) == 1
    assert second.notifications[0].pending is True
    assert notifier.incidents == []

    delivered = engine.deliver_notifications()
    assert len(delivered) == 1
    assert delivered[0].pending is False
    assert len(notifier.incidents) == 1

    evaluate_before_retry = calls["evaluate"]
    stored_value = second.status.value
    retry = engine.process(metric="profit_margin", at=t2, scope=scope)
    assert retry.idempotent is True
    assert retry.new_incidents == []
    assert retry.notifications == []
    assert retry.status.value == stored_value
    assert calls["evaluate"] == evaluate_before_retry
    assert len(notifier.incidents) == 1


def test_process_resolves_after_healthy_windows(monkeypatch):
    engine, notifier = _engine()
    scope = {"city": "Amsterdam"}
    stamps = [
        _ts(2026, 5, 15, 12, 0),
        _ts(2026, 5, 15, 12, 30),
        _ts(2026, 5, 15, 13, 0),
        _ts(2026, 5, 15, 13, 30),
    ]

    def fake_evaluate(name, at, filters=None):
        anomaly = at in stamps[:2]
        return _status(name, at, anomaly=anomaly)

    def fake_investigate(metric, at=None, **kwargs):
        status = _status("basket_cliff", at or stamps[1], anomaly=True)
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
            anomalous_metrics=[status],
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

    engine.process(metric="profit_margin", at=stamps[0], scope=scope)
    opened = engine.process(metric="profit_margin", at=stamps[1], scope=scope)
    assert opened.transition == (KPIState.DETECTED, KPIState.OPEN)
    assert len(opened.new_incidents) == 1
    engine.deliver_notifications()

    mid = engine.process(metric="profit_margin", at=stamps[2], scope=scope)
    assert mid.transition == (KPIState.OPEN, KPIState.OPEN)

    resolved = engine.process(metric="profit_margin", at=stamps[3], scope=scope)
    assert resolved.transition == (KPIState.OPEN, KPIState.RESOLVED)
    assert resolved.updated_incidents[0].state.value == "RESOLVED"
    assert any(n.kind == "incident_resolved" for n in resolved.notifications)


def test_process_quality_suppresses_without_poisoning_history(monkeypatch):
    engine, _notifier = _engine()
    t_bad = _ts(2026, 5, 15, 12, 0)
    t_good = _ts(2026, 5, 15, 12, 30)
    monkeypatch.setattr(
        engine,
        "evaluate",
        lambda name, at, filters=None: _status(name, at, anomaly=True),
    )
    monkeypatch.setattr(engine, "estimate_impact_eur", lambda *a, **k: 120.0)
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
    suppressed = engine.process(
        metric="profit_margin", at=t_bad, scope={"city": "Amsterdam"}, quality=bad
    )
    assert suppressed.transition == (KPIState.NORMAL, KPIState.SUPPRESSED)

    next_tick = engine.process(metric="profit_margin", at=t_good, scope={"city": "Amsterdam"})
    # Unhealthy observation must not count toward persistence streak.
    assert next_tick.transition == (KPIState.SUPPRESSED, KPIState.DETECTED)


def test_owner_change_while_open_enqueues_update(monkeypatch):
    engine, notifier = _engine()
    t1 = _ts(2026, 5, 15, 12, 0)
    t2 = _ts(2026, 5, 15, 12, 30)
    t3 = _ts(2026, 5, 15, 13, 0)
    scope = {"city": "Amsterdam"}
    owners = iter(["commercial-growth", "platform-ops"])

    def fake_evaluate(name, at, filters=None):
        return _status(name, at, anomaly=True)

    def fake_investigate(metric, at=None, **kwargs):
        owner = next(owners)
        status = _status("basket_cliff", at or t2, anomaly=True)
        primary = ExplanatoryCandidate(
            name="basket_cliff",
            owner=owner,
            depth=1,
            status=status,
            impact_eur=120.0,
            score=50.0,
        )
        return InvestigationResult(
            metric=metric,
            anomalous_metrics=[status],
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

    engine.process(metric="profit_margin", at=t1, scope=scope)
    opened = engine.process(metric="profit_margin", at=t2, scope=scope)
    assert opened.transition.current == KPIState.OPEN
    updated = engine.process(metric="profit_margin", at=t3, scope=scope)
    assert updated.transition == (KPIState.OPEN, KPIState.OPEN)
    assert any(n.kind == "incident_updated" for n in updated.notifications)
    engine.deliver_notifications()
    assert len(notifier.incidents) == 2


def test_acknowledge_is_sticky(monkeypatch):
    engine, _notifier = _engine()
    t1 = _ts(2026, 5, 15, 12, 0)
    t2 = _ts(2026, 5, 15, 12, 30)
    scope = {"city": "Amsterdam"}
    monkeypatch.setattr(
        engine,
        "evaluate",
        lambda name, at, filters=None: _status(name, at, anomaly=True),
    )
    monkeypatch.setattr(engine, "estimate_impact_eur", lambda *a, **k: 120.0)

    def fake_investigate(metric, at=None, **kwargs):
        status = _status("basket_cliff", at or t2, anomaly=True)
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
            anomalous_metrics=[status],
            normal_dependencies=[],
            root_candidates=[status],
            explanatory_paths=[[metric, "basket_cliff"]],
            deepest_candidates=[primary],
            primary_explanatory=primary,
            impact_eur=120.0,
        )

    monkeypatch.setattr(engine, "investigate", fake_investigate)
    engine.process(metric="profit_margin", at=t1, scope=scope)
    engine.process(metric="profit_margin", at=t2, scope=scope)
    engine.acknowledge("profit_margin", scope, at=t2)

    t3 = _ts(2026, 5, 15, 13, 0)
    stuck = engine.process(metric="profit_margin", at=t3, scope=scope)
    assert stuck.transition.current == KPIState.ACKNOWLEDGED
