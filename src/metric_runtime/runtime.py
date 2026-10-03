"""Scheduled orchestration around :class:`KPIEngine`.

    MetricRuntime   (what is due, per-tick sessions, outbox drain, run loop)
        -> KPIEngine.process   (one evaluation, authoritative lifecycle)
            -> RuntimeStore / MetricExecutor / Notifier

The scheduler cursor of a metric is the ``effective_at`` of its latest
*committed* evaluation (any outcome, including NO_DATA). Execution errors are
not committed, so their windows stay due until they fall outside
``max_catchup_windows``.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from metric_runtime.exceptions import EvaluationInProgressError, StaleEvaluationError
from metric_runtime.identity import canonical_scope_key, ensure_utc
from metric_runtime.notifications.delivery import DeliveryReport, deliver_pending
from metric_runtime.scheduling import due_ticks, next_due_at

if TYPE_CHECKING:
    from metric_runtime.config.models import RuntimeConfig
    from metric_runtime.engine import KPIEngine
    from metric_runtime.notifications.base import Notifier

__all__ = [
    "DueEvaluation",
    "MetricOutcome",
    "MetricRuntime",
    "RunReport",
    "RuntimeSchedule",
    "ScheduleOverride",
    "install_signal_handlers",
]

_log = logging.getLogger("metric_runtime.runtime")

OutcomeStatus = Literal["evaluated", "no_data", "idempotent", "skipped", "failed", "deferred"]


@dataclass(frozen=True)
class ScheduleOverride:
    interval: timedelta | None = None
    enabled: bool = True


@dataclass(frozen=True)
class RuntimeSchedule:
    """When metrics are evaluated and how long the runner may sleep."""

    default_interval: timedelta = timedelta(minutes=15)
    lag: timedelta = timedelta(0)
    max_catchup_windows: int = 1
    idle_interval: timedelta = timedelta(minutes=5)
    min_wait: timedelta = timedelta(seconds=1)
    overrides: dict[str, ScheduleOverride] = field(default_factory=dict)

    def interval_for(self, metric: str) -> timedelta:
        override = self.overrides.get(metric)
        if override is not None and override.interval is not None:
            return override.interval
        return self.default_interval

    def is_enabled(self, metric: str) -> bool:
        override = self.overrides.get(metric)
        return override is None or override.enabled

    @classmethod
    def from_config(cls, cfg: RuntimeConfig) -> RuntimeSchedule:
        from metric_runtime.config.duration import parse_duration

        return cls(
            default_interval=parse_duration(
                cfg.evaluation_interval, field="runtime.evaluation_interval"
            ),
            lag=parse_duration(cfg.evaluation_lag, field="runtime.evaluation_lag"),
            max_catchup_windows=cfg.max_catchup_windows,
            idle_interval=parse_duration(cfg.idle_interval, field="runtime.idle_interval"),
            overrides={
                metric: ScheduleOverride(
                    interval=(
                        parse_duration(s.evaluation_interval, field=f"schedules.{metric}")
                        if s.evaluation_interval
                        else None
                    ),
                    enabled=s.enabled,
                )
                for metric, s in cfg.schedules.items()
            },
        )


@dataclass(frozen=True)
class DueEvaluation:
    metric: str
    at: datetime


@dataclass(frozen=True)
class MetricOutcome:
    metric: str
    at: datetime
    status: OutcomeStatus
    transition: str | None = None
    error: str | None = None


@dataclass
class RunReport:
    """Result of one :meth:`MetricRuntime.run_once` cycle."""

    now: datetime
    scheduled_metrics: list[str] = field(default_factory=list)
    outcomes: list[MetricOutcome] = field(default_factory=list)
    catchup_skipped: dict[str, int] = field(default_factory=dict)
    delivery: DeliveryReport = field(default_factory=DeliveryReport)
    next_metric_due_at: datetime | None = None
    next_outbox_due_at: datetime | None = None

    def _with(self, status: OutcomeStatus) -> list[MetricOutcome]:
        return [o for o in self.outcomes if o.status == status]

    @property
    def evaluated(self) -> list[MetricOutcome]:
        return self._with("evaluated")

    @property
    def no_data(self) -> list[MetricOutcome]:
        return self._with("no_data")

    @property
    def skipped(self) -> list[MetricOutcome]:
        return self._with("skipped") + self._with("idempotent")

    @property
    def failed(self) -> list[MetricOutcome]:
        return self._with("failed")

    @property
    def deferred(self) -> list[MetricOutcome]:
        return self._with("deferred")

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        d = self.delivery
        return (
            f"scheduled={len(self.scheduled_metrics)} evaluated={len(self.evaluated)} "
            f"no_data={len(self.no_data)} skipped={len(self.skipped)} "
            f"failed={len(self.failed)} deferred={len(self.deferred)} "
            f"delivered={len(d.delivered)} delivery_failed={len(d.failed)} "
            f"dead_lettered={len(d.dead_lettered)}"
        )

    def to_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "now": iso(self.now),
            "ok": self.ok,
            "scheduled_metrics": list(self.scheduled_metrics),
            "outcomes": [
                {
                    "metric": o.metric,
                    "at": o.at.isoformat(),
                    "status": o.status,
                    "transition": o.transition,
                    "error": o.error,
                }
                for o in self.outcomes
            ],
            "catchup_skipped": dict(self.catchup_skipped),
            "delivery": {
                "delivered": len(self.delivery.delivered),
                "failed": len(self.delivery.failed),
                "dead_lettered": len(self.delivery.dead_lettered),
                "lease_lost": self.delivery.lease_lost,
            },
            "next_metric_due_at": iso(self.next_metric_due_at),
            "next_outbox_due_at": iso(self.next_outbox_due_at),
        }


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MetricRuntime:
    """Evaluate due metrics on a schedule and drain the notification outbox."""

    def __init__(
        self,
        engine: KPIEngine,
        schedule: RuntimeSchedule | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.schedule = schedule or RuntimeSchedule()
        self.notifier = notifier or engine.notifier
        self.clock = clock or _utcnow

    @property
    def store(self):
        return self.engine.runtime_store

    def scheduled_metrics(self) -> list[str]:
        """Enabled catalog metrics (global scope), in catalog order."""
        return [m for m in self.engine.kpi_catalog.ids() if self.schedule.is_enabled(m)]

    def ensure_ready(self) -> None:
        """Refuse to run against a runtime store with pending migrations."""
        ensure = getattr(self.store, "ensure_migrated", None)
        if callable(ensure):
            ensure()

    def due_evaluations(
        self, now: datetime | None = None
    ) -> tuple[list[DueEvaluation], dict[str, int]]:
        """Due (metric, tick) pairs from each metric's evaluation cursor.

        Returns the due list (ascending tick per metric) and, per metric, how
        many windows the catch-up cap skipped.
        """
        now = ensure_utc(now or self.clock())
        unscoped = canonical_scope_key({})
        due: list[DueEvaluation] = []
        skipped: dict[str, int] = {}
        for metric in self.scheduled_metrics():
            record = self.store.latest_committed_evaluation(metric, unscoped)
            cursor = None
            if record is not None:
                cursor = ensure_utc(record.effective_at or record.key.eval_at)
            result = due_ticks(
                cursor,
                now,
                self.schedule.interval_for(metric),
                lag=self.schedule.lag,
                max_catchup=self.schedule.max_catchup_windows,
            )
            if result.skipped_count:
                skipped[metric] = result.skipped_count
                _log.warning(
                    "metric %s: skipping %d missed window(s) %s..%s (max_catchup_windows=%d)",
                    metric,
                    result.skipped_count,
                    result.skipped_first.isoformat() if result.skipped_first else "-",
                    result.skipped_last.isoformat() if result.skipped_last else "-",
                    self.schedule.max_catchup_windows,
                )
            due.extend(DueEvaluation(metric, tick) for tick in result.ticks)
        return due, skipped

    def next_metric_due_at(self, now: datetime) -> datetime | None:
        metrics = self.scheduled_metrics()
        if not metrics:
            return None
        return min(
            next_due_at(now, self.schedule.interval_for(m), self.schedule.lag) for m in metrics
        )

    def run_once(self, now: datetime | None = None) -> RunReport:
        """Evaluate everything due at ``now`` (default: clock), then drain the outbox."""
        from metric_runtime.calculations.session import build_evaluation_context

        now = ensure_utc(now or self.clock())
        scheduled = self.scheduled_metrics()
        report = RunReport(now=now, scheduled_metrics=scheduled)
        if not scheduled:
            _log.info("no metrics scheduled; draining outbox only")

        due, report.catchup_skipped = self.due_evaluations(now)
        by_tick: dict[datetime, list[str]] = {}
        for item in due:
            by_tick.setdefault(item.at, []).append(item.metric)

        blocked: set[str] = set()
        for tick in sorted(by_tick):
            metrics = []
            for metric in by_tick[tick]:
                if metric in blocked:
                    # An earlier window of this stream failed: keep order, retry next cycle.
                    report.outcomes.append(MetricOutcome(metric, tick, "deferred"))
                else:
                    metrics.append(metric)
            if not metrics:
                continue
            session = self.engine.new_evaluation_session()
            try:
                session.evaluate_all(metrics, build_evaluation_context(at=tick))
            except Exception as exc:  # noqa: BLE001 - per-metric process() reports failures
                _log.debug("batch warm-up for tick %s failed: %s", tick.isoformat(), exc)
            for metric in metrics:
                outcome = self._process(metric, tick, session)
                report.outcomes.append(outcome)
                if outcome.status == "failed":
                    blocked.add(metric)

        report.delivery = deliver_pending(
            self.store,
            self.notifier,
            policy=self.engine.notification_policy,
            clock=self.clock,
        )
        report.next_metric_due_at = self.next_metric_due_at(now)
        report.next_outbox_due_at = self.store.next_notification_due_at(self.clock())
        if report.outcomes or report.delivery.delivered or report.delivery.failed:
            _log.info("run_once %s: %s", now.isoformat(), report.summary())
        else:
            _log.debug("run_once %s: nothing due", now.isoformat())
        return report

    def _process(self, metric: str, tick: datetime, session) -> MetricOutcome:
        try:
            result = self.engine.process(metric, at=tick, session=session)
        except (StaleEvaluationError, EvaluationInProgressError) as exc:
            _log.info("metric %s at %s skipped: %s", metric, tick.isoformat(), exc)
            return MetricOutcome(metric, tick, "skipped", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - isolate per-metric failures
            _log.error("metric %s at %s failed: %s", metric, tick.isoformat(), exc, exc_info=True)
            return MetricOutcome(metric, tick, "failed", error=f"{type(exc).__name__}: {exc}")
        transition = f"{result.transition.previous.value}->{result.transition.current.value}"
        if result.idempotent:
            status: OutcomeStatus = "idempotent"
        elif result.status.is_no_data:
            status = "no_data"
        else:
            status = "evaluated"
        return MetricOutcome(metric, tick, status, transition=transition)

    def compute_wait(self, report: RunReport, now: datetime | None = None) -> timedelta:
        """Sleep until the next metric tick or outbox retry, capped at ``idle_interval``."""
        now = ensure_utc(now or self.clock())
        idle = self.schedule.idle_interval
        candidates = [now + idle]
        for value in (report.next_metric_due_at, report.next_outbox_due_at):
            if value is not None:
                candidates.append(ensure_utc(value))
        wait = min(candidates) - now
        return max(self.schedule.min_wait, min(wait, idle))

    def _error_backoff(self, consecutive_failures: int) -> timedelta:
        delay = self.schedule.min_wait * (2 ** max(0, consecutive_failures - 1))
        return min(delay, self.schedule.idle_interval)

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        max_cycles: int | None = None,
        *,
        close_on_exit: bool = True,
    ) -> int:
        """Run cycles until ``stop_event`` is set (or ``max_cycles``). Returns cycles run."""
        stop = stop_event or threading.Event()
        if not self.scheduled_metrics():
            _log.warning(
                "no metrics scheduled (empty catalog or all disabled); outbox delivery only"
            )
        cycles = 0
        failures = 0
        try:
            while not stop.is_set():
                try:
                    report = self.run_once()
                    failures = 0
                    wait = self.compute_wait(report)
                except Exception as exc:  # noqa: BLE001 - keep the runner alive
                    failures += 1
                    wait = self._error_backoff(failures)
                    _log.error(
                        "runtime cycle failed (%d in a row), retrying in %.1fs: %s",
                        failures,
                        wait.total_seconds(),
                        exc,
                        exc_info=True,
                    )
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                _log.debug("sleeping %.1fs", wait.total_seconds())
                if stop.wait(wait.total_seconds()):
                    break
        finally:
            if close_on_exit:
                self.close()
        _log.info("runtime stopped after %d cycle(s)", cycles)
        return cycles

    def close(self) -> None:
        """Close the executor and runtime store (idempotent best effort)."""
        for resource in (self.engine.executor, self.store):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    _log.exception("error closing %r", resource)


def install_signal_handlers(stop_event: threading.Event) -> None:
    """SIGINT/SIGTERM set ``stop_event`` (main thread only)."""
    if threading.current_thread() is not threading.main_thread():
        return

    def _handler(signum, frame) -> None:  # noqa: ARG001
        _log.info("received signal %s; stopping after the current cycle", signum)
        stop_event.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # pragma: no cover - platform specific
                pass
