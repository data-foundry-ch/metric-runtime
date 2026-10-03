"""MetricRuntime.run_forever: wake-up calculation, stop event, error backoff."""

from __future__ import annotations

import signal
import threading
import time
from datetime import timedelta

import pytest

from _runtime_helpers import engine, patch_script, ts
from metric_runtime import KPIEngine
from metric_runtime.models import Incident, IncidentState, OutboxEvent
from metric_runtime.notifications import NotificationPolicy
from metric_runtime.runtime import (
    MetricRuntime,
    RunReport,
    RuntimeSchedule,
    ScheduleOverride,
    install_signal_handlers,
)
from metric_runtime.stores import InMemoryRuntimeStore

T = ts(2026, 5, 15, 12, 5)
IDLE = timedelta(minutes=5)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class RecordingStop(threading.Event):
    """Stop event that records wait timeouts and stops after ``limit`` waits."""

    def __init__(self, limit: int = 3, clock: Clock | None = None) -> None:
        super().__init__()
        self.timeouts: list[float] = []
        self.limit = limit
        self.clock = clock

    def wait(self, timeout=None) -> bool:
        self.timeouts.append(timeout)
        if self.clock is not None and timeout is not None:
            self.clock.now += timedelta(seconds=timeout)
        if len(self.timeouts) >= self.limit:
            self.set()
        return self.is_set()


def _runtime(store=None, *, interval=timedelta(minutes=15), clock=None, **schedule):
    eng = engine(store or InMemoryRuntimeStore())
    return MetricRuntime(
        eng,
        schedule=RuntimeSchedule(default_interval=interval, idle_interval=IDLE, **schedule),
        clock=clock or Clock(T),
    )


def _report(**kwargs) -> RunReport:
    return RunReport(now=T, **kwargs)


def _event(key: str = "k") -> OutboxEvent:
    return OutboxEvent(
        event_key=key,
        kind="incident_opened",
        metric="profit_margin",
        incident=Incident(
            id="inc-1",
            primary_metric="profit_margin",
            explanatory_kpi="basket_cliff",
            owner="finance",
            state=IncidentState.OPEN,
            first_detected=T,
        ),
        created_at=T,
    )


def test_outbox_retry_wins_over_later_metric_tick():
    rt = _runtime()
    report = _report(
        next_metric_due_at=T + timedelta(minutes=15),
        next_outbox_due_at=T + timedelta(seconds=30),
    )
    assert rt.compute_wait(report, T) == timedelta(seconds=30)


def test_metric_tick_wins_when_sooner():
    rt = _runtime()
    report = _report(next_metric_due_at=T + timedelta(minutes=2))
    assert rt.compute_wait(report, T) == timedelta(minutes=2)


def test_sleep_is_capped_at_idle_interval_for_long_intervals():
    rt = _runtime(interval=timedelta(days=1))
    report = _report(next_metric_due_at=rt.next_metric_due_at(T))
    assert report.next_metric_due_at - T > IDLE
    assert rt.compute_wait(report, T) == IDLE


def test_min_wait_applies_to_past_due_times():
    rt = _runtime()
    report = _report(next_outbox_due_at=T - timedelta(minutes=1))
    assert rt.compute_wait(report, T) == timedelta(seconds=1)


def test_nothing_scheduled_sleeps_idle_or_until_outbox_due():
    rt = _runtime()
    assert rt.compute_wait(_report(), T) == IDLE
    assert rt.compute_wait(_report(next_outbox_due_at=T + timedelta(seconds=40)), T) == timedelta(
        seconds=40
    )


def test_run_forever_waits_on_stop_event_with_computed_timeout(monkeypatch):
    clock = Clock(ts(2026, 5, 15, 12, 14, 30))
    rt = _runtime(clock=clock)
    patch_script(monkeypatch, rt.engine, {})
    stop = RecordingStop(limit=2, clock=clock)
    cycles = rt.run_forever(stop)
    assert cycles == 2
    # 12:14:30 -> next tick (12:00 window) due at 12:15:00.
    assert stop.timeouts[0] == pytest.approx(30.0)
    assert all(t >= 1.0 for t in stop.timeouts)


def test_outbox_retry_wakes_runner_before_metric_tick(monkeypatch):
    clock = Clock(ts(2026, 5, 15, 12, 1))
    store = InMemoryRuntimeStore()
    rt = _runtime(store, clock=clock, overrides={})
    rt.engine.notification_policy = NotificationPolicy(
        max_attempts=5, backoff_initial=timedelta(seconds=30)
    )
    patch_script(monkeypatch, rt.engine, {})

    class Flaky:
        def __init__(self):
            self.calls = 0

        def notify(self, incident, *, idempotency_key=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("webhook down")

    flaky = Flaky()
    rt.notifier = flaky
    store.enqueue_notification(_event())
    stop = RecordingStop(limit=2, clock=clock)
    rt.run_forever(stop)
    assert stop.timeouts[0] == pytest.approx(30.0)
    assert flaky.calls == 2
    assert store.list_pending_notifications() == []


def test_empty_catalog_runner_keeps_draining_outbox(caplog):
    store = InMemoryRuntimeStore()
    eng = KPIEngine([], runtime_store=store)
    clock = Clock(T)
    rt = MetricRuntime(eng, schedule=RuntimeSchedule(idle_interval=IDLE), clock=clock)
    store.enqueue_notification(_event())
    stop = RecordingStop(limit=1, clock=clock)
    with caplog.at_level("WARNING", logger="metric_runtime.runtime"):
        rt.run_forever(stop)
    assert "no metrics scheduled" in caplog.text
    assert store.list_pending_notifications() == []
    assert stop.timeouts == [IDLE.total_seconds()]


def test_all_disabled_behaves_like_empty_catalog(monkeypatch):
    rt = _runtime(
        overrides={
            "basket_cliff": ScheduleOverride(enabled=False),
            "profit_margin": ScheduleOverride(enabled=False),
        }
    )
    patch_script(monkeypatch, rt.engine, {})
    stop = RecordingStop(limit=1)
    rt.run_forever(stop)
    assert stop.timeouts == [IDLE.total_seconds()]


def test_cycle_errors_back_off_exponentially_capped_at_idle(monkeypatch):
    rt = _runtime()
    rt.schedule = RuntimeSchedule(idle_interval=timedelta(seconds=5))

    def boom(now=None):
        raise ConnectionError("store unreachable")

    monkeypatch.setattr(rt, "run_once", boom)
    stop = RecordingStop(limit=5)
    rt.run_forever(stop)
    assert stop.timeouts == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_run_forever_closes_resources():
    closed = []

    class Store(InMemoryRuntimeStore):
        def close(self):
            closed.append("store")

    class Executor:
        def close(self):
            closed.append("executor")

    eng = KPIEngine([], runtime_store=Store(), executor=Executor())
    rt = MetricRuntime(eng)
    rt.run_forever(RecordingStop(limit=1))
    assert closed == ["executor", "store"]


def test_max_cycles_stops_without_waiting():
    rt = MetricRuntime(KPIEngine([], runtime_store=InMemoryRuntimeStore()))
    stop = RecordingStop(limit=99)
    assert rt.run_forever(stop, max_cycles=1) == 1
    assert stop.timeouts == []


def test_stop_event_interrupts_real_sleep():
    rt = MetricRuntime(
        KPIEngine([], runtime_store=InMemoryRuntimeStore()),
        schedule=RuntimeSchedule(idle_interval=timedelta(minutes=10)),
    )
    stop = threading.Event()
    thread = threading.Thread(target=rt.run_forever, args=(stop,))
    started = time.monotonic()
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert time.monotonic() - started < 5


def test_signal_handler_sets_stop_event():
    stop = threading.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        install_signal_handlers(stop)
        signal.raise_signal(signal.SIGINT)
        assert stop.is_set()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
