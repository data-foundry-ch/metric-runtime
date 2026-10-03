"""MetricRuntime.run_once: cursor-driven due selection, outcomes, outbox drain."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from _runtime_helpers import engine, patch_script, ts
from metric_runtime import (
    BatchCalculation,
    BatchRegistry,
    EvaluationContext,
    KPIEngine,
    KPIState,
    Metric,
    MetricCatalog,
    StatePolicy,
)
from metric_runtime.calculations import CallableBatchSource
from metric_runtime.identity import canonical_scope_key
from metric_runtime.models import Incident, IncidentState, OutboxEvent
from metric_runtime.notifications import RecordingNotifier
from metric_runtime.runtime import MetricRuntime, RuntimeSchedule, ScheduleOverride

M15 = timedelta(minutes=15)
T12 = ts(2026, 5, 15, 12, 0)
UNSCOPED = canonical_scope_key()


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _runtime(store, *, clock=None, notifier=None, **schedule) -> MetricRuntime:
    eng = engine(store, notifier=notifier)
    return MetricRuntime(
        eng,
        schedule=RuntimeSchedule(default_interval=M15, **schedule),
        clock=clock or Clock(T12),
    )


def _statuses(report) -> dict[tuple[str, object], str]:
    return {(o.metric, o.at): o.status for o in report.outcomes}


def test_due_selection_follows_the_evaluation_cursor(store_factory, monkeypatch):
    rt = _runtime(store_factory())
    calls = patch_script(monkeypatch, rt.engine, {})
    now = ts(2026, 5, 15, 12, 30)

    first = rt.run_once(now)
    assert {(o.metric, o.at) for o in first.evaluated} == {
        ("basket_cliff", ts(2026, 5, 15, 12, 15)),
        ("profit_margin", ts(2026, 5, 15, 12, 15)),
    }
    assert first.ok
    # Same instant again: nothing is due (healthy, unchanged-state windows are not re-run).
    second = rt.run_once(now)
    assert second.outcomes == []
    assert len(calls) == 2
    # Next tick becomes due once its window closes.
    third = rt.run_once(ts(2026, 5, 15, 12, 45))
    assert [o.at for o in third.evaluated] == [ts(2026, 5, 15, 12, 30)] * 2
    assert third.next_metric_due_at == ts(2026, 5, 15, 13, 0)


def test_catchup_replays_missed_windows_in_order(store_factory, monkeypatch):
    rt = _runtime(store_factory(), max_catchup_windows=3)
    patch_script(monkeypatch, rt.engine, {})
    rt.run_once(ts(2026, 5, 15, 12, 15))  # cursor at 12:00
    report = rt.run_once(ts(2026, 5, 15, 13, 15))  # 12:15..13:00 due, cap 3
    ticks = [o.at for o in report.outcomes if o.metric == "profit_margin"]
    assert ticks == [ts(2026, 5, 15, 12, 30), ts(2026, 5, 15, 12, 45), ts(2026, 5, 15, 13, 0)]
    assert report.catchup_skipped == {"basket_cliff": 1, "profit_margin": 1}


def test_no_data_window_is_committed_and_not_rerun(store_factory, monkeypatch):
    store = store_factory()
    rt = _runtime(store)
    tick = ts(2026, 5, 15, 12, 15)
    calls = patch_script(monkeypatch, rt.engine, {tick: "no_data"})
    now = ts(2026, 5, 15, 12, 30)

    report = rt.run_once(now)
    assert {o.status for o in report.outcomes} == {"no_data"}
    assert report.ok
    assert store.latest_committed_evaluation("profit_margin", UNSCOPED).effective_at == tick
    assert rt.run_once(now).outcomes == []
    assert len(calls) == 2


def test_open_incident_stays_open_across_no_data_gap(store_factory, monkeypatch):
    store = store_factory()
    rt = _runtime(store)
    t1, t2, t3 = (ts(2026, 5, 15, 12, m) for m in (0, 15, 30))
    patch_script(
        monkeypatch,
        rt.engine,
        lambda name, at: (
            {t1: "anomaly", t2: "anomaly", t3: "no_data"}.get(at, "healthy")
            if name == "profit_margin"
            else "healthy"
        ),
    )
    rt.run_once(t1 + M15)
    opened = rt.run_once(t2 + M15)
    assert _statuses(opened)[("profit_margin", t2)] == "evaluated"
    assert store.get_state_record("profit_margin", UNSCOPED).state == KPIState.OPEN
    incident = store.find_active_incident("profit_margin", UNSCOPED)
    assert incident is not None
    delivered_before = len(opened.delivery.delivered)
    assert delivered_before == 1

    gap = rt.run_once(t3 + M15)
    assert _statuses(gap)[("profit_margin", t3)] == "no_data"
    assert gap.delivery.delivered == []
    record = store.get_state_record("profit_margin", UNSCOPED)
    assert record.state == KPIState.OPEN
    assert record.last_evaluation_at == t3
    assert store.find_active_incident("profit_margin", UNSCOPED).id == incident.id


def test_execution_error_is_retried_within_catchup_then_abandoned(store_factory, monkeypatch):
    store = store_factory()
    rt = _runtime(store, max_catchup_windows=2)
    bad = ts(2026, 5, 15, 12, 15)
    calls = patch_script(
        monkeypatch,
        rt.engine,
        lambda name, at: "error" if (name == "profit_margin" and at == bad) else "healthy",
    )
    rt.run_once(ts(2026, 5, 15, 12, 15))  # cursor 12:00 for both

    first = rt.run_once(ts(2026, 5, 15, 12, 30))
    assert _statuses(first)[("profit_margin", bad)] == "failed"
    assert _statuses(first)[("basket_cliff", bad)] == "evaluated"
    assert not first.ok
    assert store.latest_committed_evaluation("profit_margin", UNSCOPED).effective_at == ts(
        2026, 5, 15, 12, 0
    )

    # Still inside the catch-up window: retried first, and later ticks of the
    # stream are deferred to keep effective_at order.
    second = rt.run_once(ts(2026, 5, 15, 12, 45))
    statuses = _statuses(second)
    assert statuses[("profit_margin", bad)] == "failed"
    assert statuses[("profit_margin", ts(2026, 5, 15, 12, 30))] == "deferred"
    assert statuses[("basket_cliff", ts(2026, 5, 15, 12, 30))] == "evaluated"

    # The bad window falls outside max_catchup_windows: abandoned, stream resumes.
    third = rt.run_once(ts(2026, 5, 15, 13, 15))
    statuses = _statuses(third)
    assert ("profit_margin", bad) not in statuses
    assert statuses[("profit_margin", ts(2026, 5, 15, 12, 45))] == "evaluated"
    assert statuses[("profit_margin", ts(2026, 5, 15, 13, 0))] == "evaluated"
    assert third.catchup_skipped["profit_margin"] == 2
    assert third.ok
    assert Counter(at for name, at in calls if name == "profit_margin")[bad] == 2


def test_failure_is_isolated_per_metric(store_factory, monkeypatch):
    rt = _runtime(store_factory())
    patch_script(
        monkeypatch, rt.engine, lambda name, at: "error" if name == "basket_cliff" else "healthy"
    )
    report = rt.run_once(ts(2026, 5, 15, 12, 30))
    statuses = {o.metric: o.status for o in report.outcomes}
    assert statuses == {"basket_cliff": "failed", "profit_margin": "evaluated"}
    assert "basket_cliff failed" in report.failed[0].error


def test_disabled_metrics_are_not_scheduled(store_factory, monkeypatch):
    rt = _runtime(store_factory(), overrides={"basket_cliff": ScheduleOverride(enabled=False)})
    patch_script(monkeypatch, rt.engine, {})
    report = rt.run_once(ts(2026, 5, 15, 12, 30))
    assert report.scheduled_metrics == ["profit_margin"]
    assert {o.metric for o in report.outcomes} == {"profit_margin"}


def test_per_metric_interval_override(store_factory, monkeypatch):
    rt = _runtime(
        store_factory(),
        overrides={"basket_cliff": ScheduleOverride(interval=timedelta(hours=1))},
    )
    patch_script(monkeypatch, rt.engine, {})
    report = rt.run_once(ts(2026, 5, 15, 12, 30))
    ticks = {o.metric: o.at for o in report.outcomes}
    assert ticks == {
        "basket_cliff": ts(2026, 5, 15, 11, 0),
        "profit_margin": ts(2026, 5, 15, 12, 15),
    }


def test_all_disabled_still_drains_outbox(store_factory):
    store = store_factory()
    notifier = RecordingNotifier()
    rt = _runtime(
        store,
        notifier=notifier,
        overrides={
            "basket_cliff": ScheduleOverride(enabled=False),
            "profit_margin": ScheduleOverride(enabled=False),
        },
    )
    store.enqueue_notification(
        OutboxEvent(
            event_key="k",
            kind="incident_opened",
            metric="profit_margin",
            incident=Incident(
                id="inc-1",
                primary_metric="profit_margin",
                explanatory_kpi="basket_cliff",
                owner="finance",
                state=IncidentState.OPEN,
                first_detected=T12,
            ),
            created_at=T12,
        )
    )
    report = rt.run_once()
    assert report.scheduled_metrics == []
    assert report.outcomes == []
    assert report.ok
    assert len(report.delivery.delivered) == 1
    assert report.next_metric_due_at is None
    assert notifier.idempotency_keys == ["k"]


def test_empty_catalog_run_once_succeeds(store_factory):
    eng = KPIEngine([], runtime_store=store_factory())
    report = MetricRuntime(eng, clock=Clock(T12)).run_once()
    assert report.ok and report.outcomes == [] and report.next_metric_due_at is None


def _batch_runtime(store, handler) -> MetricRuntime:
    registry = BatchRegistry()
    registry.register("numbers", CallableBatchSource("numbers", handler))
    catalog = MetricCatalog(
        [
            Metric(id="a", name="A", calculation=BatchCalculation(source="numbers", result="a")),
            Metric(id="b", name="B", calculation=BatchCalculation(source="numbers", result="b")),
        ]
    )
    eng = KPIEngine(
        catalog,
        runtime_store=store,
        batch_registry=registry,
        state_policy=StatePolicy(persistence=1, min_impact=0.0),
    )
    return MetricRuntime(eng, schedule=RuntimeSchedule(default_interval=M15), clock=Clock(T12))


def test_shared_batch_source_runs_once_per_context_per_tick(store_factory):
    seen: Counter = Counter()

    def handler(ctx: EvaluationContext) -> dict[str, float | None]:
        seen[ctx.effective_at] += 1
        return {"a": 10.0, "b": 20.0}

    rt = _batch_runtime(store_factory(), handler)
    report = rt.run_once(ts(2026, 5, 15, 12, 30))
    assert {o.metric for o in report.evaluated} == {"a", "b"}
    assert seen  # current + baseline windows
    assert set(seen.values()) == {1}


def test_real_no_data_from_calculation_is_committed(store_factory):
    def handler(ctx: EvaluationContext) -> dict[str, float | None]:
        return {"a": None, "b": 5.0}

    store = store_factory()
    rt = _batch_runtime(store, handler)
    now = ts(2026, 5, 15, 12, 30)
    report = rt.run_once(now)
    statuses = {o.metric: o.status for o in report.outcomes}
    assert statuses["a"] == "no_data"
    assert report.ok
    assert store.latest_committed_evaluation("a", UNSCOPED) is not None
    assert rt.run_once(now).outcomes == []
