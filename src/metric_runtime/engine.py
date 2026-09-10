"""KPI evaluation engine.

Python API first. Configuration files for deployment.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from metric_runtime.catalog import KPICatalog
from metric_runtime.detectors import DetectorStrategy, SeasonalZScoreDetector
from metric_runtime.detectors.specs import build_detector
from metric_runtime.exceptions import MetricRuntimeError, UnknownMetricError
from metric_runtime.identity import EvaluationKey, ensure_utc
from metric_runtime.models import (
    KPI,
    DetectorConfig,
    DrilldownRow,
    IncidentState,
    KPIState,
    KPIStatus,
    MeasureRef,
    MetricStateRecord,
    OutboxEvent,
    ProcessResult,
    QualityReport,
    StoredObservation,
)
from metric_runtime.notifications.base import Notifier, NullNotifier
from metric_runtime.state import KPIStateTransition, StatePolicy, evolve_state
from metric_runtime.stores.memory import InMemoryStateStore


def _resolve_detector(
    metric: KPI,
    engine_default: DetectorStrategy,
) -> tuple[DetectorStrategy, DetectorConfig]:
    """Return (strategy, config) for a KPI.

    KPI.detector is a serializable DetectorSpec (SeasonalZScore | Threshold).
    The engine builds the runtime DetectorStrategy via the registry/factory.
    """
    configured = metric.detector
    if configured is None:
        return engine_default, engine_default.as_config()
    strategy = build_detector(configured)
    return strategy, strategy.as_config()


class KPIEngine:
    """Evaluate KPIs, detect anomalies, and drive investigation."""

    def __init__(
        self,
        catalog: KPICatalog | dict[str, KPI] | list[KPI],
        executor=None,
        state_store=None,
        detector: DetectorStrategy | None = None,
        notifier: Notifier | None = None,
        *,
        connection=None,
        fact_table: str | None = None,
        state_policy: StatePolicy | None = None,
        preferred_leaves: tuple[str, ...] = (),
    ):
        if isinstance(catalog, KPICatalog):
            self._catalog = catalog
        elif isinstance(catalog, dict):
            self._catalog = KPICatalog(catalog)
        else:
            self._catalog = KPICatalog(list(catalog))

        if connection is not None and executor is None:
            from metric_runtime.execution.duckdb import DuckDBExecutor

            if not fact_table:
                raise MetricRuntimeError(
                    "KPIEngine(connection=...) requires explicit fact_table=..."
                )
            executor = DuckDBExecutor(connection, fact_table=fact_table)

        self.executor = executor
        self.state_store = state_store or InMemoryStateStore()
        self.detector = detector or SeasonalZScoreDetector()
        self.notifier = notifier or NullNotifier()
        self.state_policy = state_policy or StatePolicy()
        self.preferred_leaves = preferred_leaves

        # Convenience attributes used by example quality helpers.
        self.fact_table = getattr(executor, "fact_table", fact_table)
        self.con = getattr(executor, "con", connection)

    @property
    def catalog(self) -> dict[str, KPI]:
        """Dict-like catalog for back-compat with demo/tests."""
        return self._catalog.as_dict()

    @property
    def catalog_dict(self) -> dict[str, KPI]:
        return self._catalog.as_dict()

    @property
    def kpi_catalog(self) -> KPICatalog:
        return self._catalog

    @classmethod
    def from_profile(
        cls,
        profile: str,
        *,
        project_config: str | Path | None = None,
        connections_config: str | Path | None = None,
        catalog: KPICatalog | dict[str, KPI] | list[KPI] | None = None,
    ) -> KPIEngine:
        from metric_runtime.config.factory import build_runtime

        return build_runtime(
            profile,
            project_config=project_config,
            connections_config=connections_config,
            catalog=catalog,
        )

    def _require_executor(self):
        if self.executor is None:
            raise MetricRuntimeError(
                "KPIEngine has no executor. Pass DuckDBExecutor(...) "
                "or build via KPIEngine.from_profile(...)."
            )
        return self.executor

    def _get_kpi(self, metric_name: str) -> KPI:
        try:
            return self._catalog.get(metric_name)
        except UnknownMetricError:
            raise

    def metric_value(
        self,
        metric_name: str,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float:
        metric = self._get_kpi(metric_name)
        if metric.formula is None:
            raise MetricRuntimeError(f"KPI {metric_name!r} has no formula for execution")
        return self._require_executor().metric_value(
            metric.formula, at=at, filters=filters, start=start, end=end
        )

    def measure_value(
        self,
        measure: MeasureRef,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float:
        return self._require_executor().measure_value(
            measure, at=at, filters=filters, start=start, end=end
        )

    def support_value(
        self,
        metric_name: str,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float:
        """Return support measure value when configured; otherwise 0.0.

        No domain-default support column is assumed. KPIs that need a
        minimum-support gate must declare ``support=SupportRequirement(...)``.
        """
        metric = self._get_kpi(metric_name)
        if metric.support is None:
            return 0.0
        return self.measure_value(metric.support.measure, at, filters, start, end)

    def baseline_values(
        self,
        metric_name: str,
        at: datetime,
        filters: dict[str, str] | None = None,
    ) -> list[float]:
        metric = self._get_kpi(metric_name)
        _, cfg = _resolve_detector(metric, self.detector)
        return [
            self.metric_value(metric_name, at - timedelta(weeks=i), filters)
            for i in range(1, cfg.baseline_weeks + 1)
        ]

    def evaluate(
        self,
        metric_name: str,
        at: datetime,
        filters: dict[str, str] | None = None,
    ) -> KPIStatus:
        metric = self._get_kpi(metric_name)
        strategy, cfg = _resolve_detector(metric, self.detector)
        current = self.metric_value(metric_name, at, filters)
        baseline = self.baseline_values(metric_name, at, filters)
        support = self.support_value(metric_name, at, filters)
        support_ok = True
        if metric.support is not None:
            support_ok = support >= metric.support.minimum

        return strategy.evaluate(
            metric_name,
            current,
            baseline,
            directionality=metric.directionality,
            config=cfg,
            support=support,
            support_ok=support_ok,
            as_of=at,
        )

    def evaluate_window(
        self,
        metric_name: str,
        start: datetime,
        end: datetime,
        filters: dict[str, str] | None = None,
        baseline_weeks: int | None = None,
    ) -> KPIStatus:
        """Compare a multi-interval window to same windows in prior weeks."""
        metric = self._get_kpi(metric_name)
        strategy, cfg = _resolve_detector(metric, self.detector)
        weeks = baseline_weeks if baseline_weeks is not None else cfg.baseline_weeks
        current = self.metric_value(metric_name, filters=filters, start=start, end=end)
        baseline = [
            self.metric_value(
                metric_name,
                filters=filters,
                start=start - timedelta(weeks=i),
                end=end - timedelta(weeks=i),
            )
            for i in range(1, weeks + 1)
        ]
        support = self.support_value(metric_name, filters=filters, start=start, end=end)
        support_ok = True
        if metric.support is not None:
            support_ok = support >= metric.support.minimum

        return strategy.evaluate(
            metric_name,
            current,
            baseline,
            directionality=metric.directionality,
            config=cfg,
            support=support,
            support_ok=support_ok,
            as_of=end,
        )

    def estimate_impact_eur(
        self,
        metric_name: str,
        at: datetime,
        current_value: float,
        baseline_value: float,
        filters: dict[str, str] | None = None,
    ) -> float:
        metric = self._get_kpi(metric_name)
        kind = metric.impact.kind

        if kind == "none":
            return 0.0
        if kind in {"margin_delta", "revenue_delta"}:
            return max(0.0, baseline_value - current_value)
        if kind == "cost_delta":
            return max(0.0, current_value - baseline_value)
        if kind == "quantity_delta":
            delta = baseline_value - current_value
            if delta <= 0:
                return 0.0
            unit_metric = metric.impact.unit_value_metric
            if unit_metric and unit_metric in self._catalog:
                return delta * self.metric_value(unit_metric, at, filters)
            return delta
        return 0.0

    def drilldown(
        self,
        metric_name: str,
        at: datetime,
        dimensions: Iterable[str],
        only_anomalies: bool = True,
        parent_filters: dict[str, str] | None = None,
    ) -> list[DrilldownRow]:
        dims = tuple(dimensions)
        metric = self._get_kpi(metric_name)
        executor = self._require_executor()
        metric_dims = set(metric.dimensions)
        if metric_dims and not set(dims).issubset(metric_dims):
            raise ValueError(f"{metric_name} only supports dimensions {metric.dimensions}")

        allowed = metric_dims or getattr(executor, "valid_dimensions", None)
        rows: list[DrilldownRow] = []
        for filters in executor.distinct_groups(
            dims, at=at, filters=parent_filters, valid_dimensions=allowed
        ):
            status = self.evaluate(metric_name, at, filters)
            if only_anomalies and not status.anomaly:
                continue
            impact_eur = self.estimate_impact_eur(
                metric_name,
                at,
                status.value,
                status.baseline_mean,
                filters,
            )
            rows.append(
                DrilldownRow(
                    filters=filters,
                    value=status.value,
                    baseline_mean=status.baseline_mean,
                    relative_change=status.relative_change,
                    z_score=status.z_score,
                    anomaly=status.anomaly,
                    support=status.support,
                    impact_eur=impact_eur,
                )
            )
        rows.sort(key=lambda row: row.impact_eur, reverse=True)
        return rows

    def investigate(
        self,
        metric: str,
        at: datetime | None = None,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        filters: dict[str, str] | None = None,
        preferred_leaves: tuple[str, ...] = (),
    ):
        from metric_runtime.investigation import investigate_metric, investigate_window

        leaves = preferred_leaves or self.preferred_leaves
        if start is not None and end is not None:
            return investigate_window(
                self,
                metric,
                start,
                end,
                filters,
                preferred_leaves=leaves,
            )
        if at is None:
            raise MetricRuntimeError("investigate() requires at= or start=/end=")
        return investigate_metric(self, metric, at, filters, preferred_leaves=leaves)

    def acknowledge(
        self,
        metric: str,
        scope: dict[str, str] | None = None,
        *,
        at: datetime | None = None,
    ) -> KPIState:
        """Explicit human acknowledgement — sticky while the anomaly persists."""

        from metric_runtime.identity import canonical_scope_key

        scope = dict(scope or {})
        scope_key = canonical_scope_key(scope)
        stamp = ensure_utc(at) if at is not None else datetime.now(UTC)
        previous = self.state_store.get_state_record(metric, scope_key)
        self.state_store.set_state_record(
            MetricStateRecord(
                metric=metric,
                scope_key=scope_key,
                scope=scope,
                state=KPIState.ACKNOWLEDGED,
                state_since=stamp
                if previous.state != KPIState.ACKNOWLEDGED
                else previous.state_since,
                updated_at=stamp,
                resolved_at=previous.resolved_at,
            )
        )
        active = self.state_store.find_active_incident(metric, scope_key)
        if active is not None:
            updated = active.model_copy(
                update={"state": IncidentState.ACKNOWLEDGED, "updated_at": stamp}
            )
            self.state_store.upsert_incident(updated)
        return KPIState.ACKNOWLEDGED

    def deliver_notifications(self, *, limit: int | None = None) -> list[OutboxEvent]:
        """Delivery worker: send pending outbox events through the notifier."""

        pending = self.state_store.list_pending_notifications()
        if limit is not None:
            pending = pending[:limit]
        delivered: list[OutboxEvent] = []
        for event in pending:
            if event.incident is not None:
                self.notifier.notify(event.incident)
            assert event.id is not None
            delivered.append(
                self.state_store.mark_notification_delivered(event.id, at=datetime.now(UTC))
            )
        return delivered

    def process(
        self,
        metric: str,
        *,
        at: datetime | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        scope: dict[str, str] | None = None,
        quality: QualityReport | None = None,
        preferred_leaves: tuple[str, ...] = (),
        context: list[str] | None = None,
    ) -> ProcessResult:
        """Authoritative runtime tick: observe → persist → state → incident → outbox.

        True idempotency: EvaluationKey is computed first; a stored observation
        short-circuits before warehouse evaluation. Notifications are enqueued
        to an outbox (call ``deliver_notifications`` to send).
        """

        from metric_runtime.incidents import incident_from_investigation

        scope = dict(scope or {})
        leaves = preferred_leaves or self.preferred_leaves
        policy = self.state_policy
        eval_key = EvaluationKey.build(metric, scope=scope, at=at, start=start, end=end)
        scope_key = eval_key.scope_key
        eval_at = eval_key.eval_at

        existing = self.state_store.get_observation(eval_key)
        if existing is not None:
            record = self.state_store.get_state_record(metric, scope_key)
            return ProcessResult(
                metric=metric,
                scope=scope,
                evaluation_key=eval_key,
                at=eval_at,
                status=existing.status,
                transition=KPIStateTransition(record.state, record.state),
                idempotent=True,
            )

        if start is not None and end is not None:
            status = self.evaluate_window(metric, start, end, scope)
        else:
            assert at is not None
            status = self.evaluate(metric, at, scope)

        quality_ok = True if quality is None else quality.healthy
        eligible = quality_ok
        stored_obs = self.state_store.put_observation(
            StoredObservation(
                key=eval_key,
                status=status,
                eligible_for_state=eligible,
                quality_healthy=None if quality is None else quality.healthy,
                recorded_at=datetime.now(UTC),
            )
        )

        previous_record = self.state_store.get_state_record(metric, scope_key)
        previous = previous_record.state
        eligible_history = [
            item.status
            for item in self.state_store.get_history(metric, scope_key)
            if item.eligible_for_state
        ]
        impact = self.estimate_impact_eur(
            metric, eval_at, status.value, status.baseline_mean, scope
        )

        current = evolve_state(
            eligible_history,
            policy=policy,
            impact_eur=impact,
            quality=quality,
            previous=previous,
        )

        if previous == KPIState.RESOLVED and current in {KPIState.DETECTED, KPIState.OPEN}:
            if previous_record.resolved_at is not None:
                delta = eval_at - ensure_utc(previous_record.resolved_at)
                if delta < timedelta(minutes=policy.cooldown_minutes):
                    current = KPIState.RESOLVED

        now = datetime.now(UTC)
        resolved_at = previous_record.resolved_at
        state_since = previous_record.state_since
        if previous != current:
            state_since = now
        if current == KPIState.RESOLVED:
            resolved_at = eval_at
        self.state_store.set_state_record(
            MetricStateRecord(
                metric=metric,
                scope_key=scope_key,
                scope=scope,
                state=current,
                state_since=state_since,
                updated_at=now,
                resolved_at=resolved_at,
            )
        )

        transition = KPIStateTransition(previous, current)
        new_incidents: list = []
        updated_incidents: list = []
        notifications: list[OutboxEvent] = []
        investigation = None
        active = self.state_store.find_active_incident(metric, scope_key)

        def _enqueue(event: OutboxEvent) -> OutboxEvent:
            queued = self.state_store.enqueue_notification(event)
            notifications.append(queued)
            return queued

        if current == KPIState.OPEN:
            investigation = self.investigate(
                metric, at=eval_at, filters=scope, preferred_leaves=leaves
            )
            first_detected = (
                active.first_detected
                if active is not None
                else next(
                    (h.as_of for h in eligible_history if h.anomaly and h.support_ok),
                    status.as_of,
                )
            )
            draft = incident_from_investigation(
                self,
                center_kpi=metric,
                scope=scope,
                at=eval_at,
                inv=investigation,
                state=IncidentState.OPEN,
                first_detected=first_detected,
                estimated_impact=impact,
                persistence_windows=policy.persistence,
                context=context,
                preferred_leaves=leaves,
            )
            if active is None:
                stored = self.state_store.upsert_incident(draft)
                new_incidents.append(stored)
                _enqueue(
                    OutboxEvent(
                        kind="incident_opened",
                        incident=stored,
                        metric=metric,
                        previous_state=previous,
                        current_state=current,
                        message=f"Opened incident for {metric}",
                        created_at=now,
                    )
                )
            else:
                merged = draft.model_copy(
                    update={
                        "id": active.id,
                        "first_detected": active.first_detected,
                        "opened_at": active.opened_at or draft.opened_at,
                        "state": IncidentState.OPEN,
                    }
                )
                meaningful = (
                    previous != current
                    or active.explanatory_kpi != merged.explanatory_kpi
                    or active.owner != merged.owner
                    or active.state != IncidentState.OPEN
                )
                stored = self.state_store.upsert_incident(merged)
                updated_incidents.append(stored)
                if meaningful:
                    _enqueue(
                        OutboxEvent(
                            kind="incident_updated",
                            incident=stored,
                            metric=metric,
                            previous_state=previous,
                            current_state=current,
                            message=f"Updated incident for {metric}",
                            created_at=now,
                        )
                    )

        elif current == KPIState.RESOLVED and active is not None:
            resolved = active.model_copy(
                update={"state": IncidentState.RESOLVED, "updated_at": eval_at}
            )
            stored = self.state_store.upsert_incident(resolved)
            updated_incidents.append(stored)
            if previous != current:
                _enqueue(
                    OutboxEvent(
                        kind="incident_resolved",
                        incident=stored,
                        metric=metric,
                        previous_state=previous,
                        current_state=current,
                        message=f"Resolved incident for {metric}",
                        created_at=now,
                    )
                )

        elif current == KPIState.SUPPRESSED and previous != current:
            _enqueue(
                OutboxEvent(
                    kind="state_changed",
                    metric=metric,
                    previous_state=previous,
                    current_state=current,
                    message=f"Suppressed {metric} due to data quality",
                    created_at=now,
                )
            )
            if active is not None:
                suppressed = active.model_copy(
                    update={"state": IncidentState.SUPPRESSED, "updated_at": eval_at}
                )
                stored = self.state_store.upsert_incident(suppressed)
                updated_incidents.append(stored)

        _ = stored_obs  # persisted before side effects

        return ProcessResult(
            metric=metric,
            scope=scope,
            evaluation_key=eval_key,
            at=eval_at,
            status=status,
            transition=transition,
            new_incidents=new_incidents,
            updated_incidents=updated_incidents,
            notifications=notifications,
            investigation=investigation,
            idempotent=False,
        )

    # Alias used by schedulers / workers.
    tick = process
