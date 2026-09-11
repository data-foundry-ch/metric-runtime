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
from metric_runtime.exceptions import (
    EvaluationInProgressError,
    MetricRuntimeError,
    UnknownMetricError,
)
from metric_runtime.identity import EvaluationKey, ensure_utc
from metric_runtime.models import (
    KPI,
    DetectorConfig,
    DrilldownRow,
    EvaluationRecord,
    IncidentState,
    KPIState,
    KPIStateTransition,
    KPIStatus,
    MeasureRef,
    MetricStateRecord,
    OutboxEvent,
    ProcessResult,
    QualityReport,
    StoredObservation,
)
from metric_runtime.notifications.base import Notifier, NullNotifier
from metric_runtime.state import StatePolicy, evolve_state, signals_from_history
from metric_runtime.stores.base import EvaluationClaimStatus
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
        at = ensure_utc(at)
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
        start = ensure_utc(start)
        end = ensure_utc(end)
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
                state_since=(
                    stamp if previous.state != KPIState.ACKNOWLEDGED else previous.state_since
                ),
                updated_at=stamp,
                opened_at=previous.opened_at,
                acknowledged_at=stamp,
                resolved_at=previous.resolved_at,
                detection_streak=previous.detection_streak,
                healthy_streak=previous.healthy_streak,
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
        """Delivery worker: send pending outbox events through the notifier.

        External delivery is at-least-once. Failures are recorded on the event
        and do not block later events. Returns events successfully marked delivered
        in this pass.
        """
        pending = self.state_store.list_pending_notifications()
        if limit is not None:
            pending = pending[:limit]
        delivered: list[OutboxEvent] = []
        for event in pending:
            now = datetime.now(UTC)
            try:
                if event.incident is not None:
                    self.notifier.notify(
                        event.incident,
                        idempotency_key=event.event_key or event.id,
                    )
            except Exception as exc:  # noqa: BLE001 - record and continue
                assert event.id is not None
                self.state_store.record_notification_attempt(
                    event.id,
                    at=now,
                    error=str(exc)[:500],
                )
                continue
            assert event.id is not None
            delivered.append(self.state_store.mark_notification_delivered(event.id, at=now))
        return delivered

    def _result_from_record(self, record: EvaluationRecord) -> ProcessResult:
        """Reconstruct an idempotent ProcessResult from a committed record."""
        return ProcessResult(
            metric=record.key.metric,
            scope=dict(record.key.scope),
            evaluation_key=record.key,
            at=record.key.eval_at,
            status=record.status,
            transition=record.transition,
            new_incidents=[],
            updated_incidents=[],
            notifications=[],
            investigation=record.investigation,
            idempotent=True,
            evaluation_record=record,
        )

    @staticmethod
    def _outbox_event_key(
        *,
        kind: str,
        metric: str,
        incident_id: str | None,
        previous: KPIState,
        current: KPIState,
        eval_identity: str,
    ) -> str:
        import hashlib

        raw = (
            f"{kind}|{metric}|{incident_id or ''}|{previous.value}|{current.value}|{eval_identity}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

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
        """Authoritative runtime tick with atomic commit semantics.

        Calculate once. Commit once. Act from committed state.
        """
        from metric_runtime.incidents import incident_from_investigation
        from metric_runtime.state import (
            _trailing_detection_streak,
            _trailing_healthy_streak,
        )

        scope = dict(scope or {})
        leaves = preferred_leaves or self.preferred_leaves
        policy = self.state_policy
        eval_key = EvaluationKey.build(metric, scope=scope, at=at, start=start, end=end)
        scope_key = eval_key.scope_key
        eval_at = eval_key.eval_at

        claim = self.state_store.claim_evaluation(eval_key)
        if claim.status == EvaluationClaimStatus.ALREADY_COMMITTED:
            assert claim.record is not None
            return self._result_from_record(claim.record)
        if claim.status == EvaluationClaimStatus.IN_PROGRESS:
            raise EvaluationInProgressError(
                f"Evaluation already in progress for {eval_key.identity}"
            )

        try:
            if start is not None and end is not None:
                status = self.evaluate_window(metric, start, end, scope)
            else:
                assert at is not None
                status = self.evaluate(metric, at, scope)

            quality_ok = True if quality is None else quality.healthy
            now = datetime.now(UTC)
            observation = StoredObservation(
                key=eval_key,
                status=status,
                eligible_for_state=quality_ok,
                quality_healthy=None if quality is None else quality.healthy,
                recorded_at=now,
            )

            previous_record = self.state_store.get_state_record(metric, scope_key)
            previous = previous_record.state
            history = list(self.state_store.get_history(metric, scope_key)) + [observation]
            signals = signals_from_history(history)
            impact = self.estimate_impact_eur(
                metric, eval_at, status.value, status.baseline_mean, scope
            )
            current = evolve_state(
                signals,
                policy=policy,
                impact_eur=impact,
                quality=quality,
                previous=previous,
            )
            if previous == KPIState.RESOLVED and current in {
                KPIState.DETECTED,
                KPIState.OPEN,
            }:
                if previous_record.resolved_at is not None:
                    delta = eval_at - ensure_utc(previous_record.resolved_at)
                    if delta < timedelta(minutes=policy.cooldown_minutes):
                        current = KPIState.RESOLVED

            detection_streak = _trailing_detection_streak(signals)
            healthy_streak = _trailing_healthy_streak(signals)
            resolved_at = previous_record.resolved_at
            opened_at = previous_record.opened_at
            acknowledged_at = previous_record.acknowledged_at
            state_since = previous_record.state_since
            if previous != current:
                state_since = now
            if current == KPIState.RESOLVED:
                resolved_at = eval_at
            if current == KPIState.OPEN:
                opened_at = opened_at or eval_at
            if current == KPIState.ACKNOWLEDGED:
                acknowledged_at = acknowledged_at or eval_at
            state_record = MetricStateRecord(
                metric=metric,
                scope_key=scope_key,
                scope=scope,
                state=current,
                state_since=state_since,
                updated_at=now,
                opened_at=opened_at,
                acknowledged_at=acknowledged_at,
                resolved_at=resolved_at,
                detection_streak=detection_streak,
                healthy_streak=healthy_streak,
            )
            transition = KPIStateTransition(previous=previous, current=current)

            investigation = None
            draft_incident = None
            merge_active = None
            outbox_drafts: list[OutboxEvent] = []
            active = self.state_store.find_active_incident(metric, scope_key)

            if current == KPIState.OPEN:
                investigation = self.investigate(
                    metric, at=eval_at, filters=scope, preferred_leaves=leaves
                )
                first_detected = (
                    active.first_detected
                    if active is not None
                    else next(
                        (
                            item.status.as_of
                            for item in history
                            if item.eligible_for_state
                            and item.status.anomaly
                            and item.status.support_ok
                        ),
                        status.as_of,
                    )
                )
                draft_incident = incident_from_investigation(
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
                merge_active = active

            elif current == KPIState.RESOLVED and active is not None:
                draft_incident = active.model_copy(
                    update={"state": IncidentState.RESOLVED, "updated_at": eval_at}
                )
                merge_active = active

            elif current == KPIState.SUPPRESSED and previous != current and active is not None:
                draft_incident = active.model_copy(
                    update={"state": IncidentState.SUPPRESSED, "updated_at": eval_at}
                )
                merge_active = active

            with self.state_store.transaction() as tx:
                tx.stage_observation(observation)
                tx.stage_state_record(state_record)

                new_incident_ids: list[str] = []
                updated_incident_ids: list[str] = []
                staged_incidents: list = []

                if current == KPIState.OPEN and draft_incident is not None:
                    if merge_active is None:
                        stored = tx.stage_incident(draft_incident)
                        staged_incidents.append(stored)
                        new_incident_ids.append(stored.id)  # type: ignore[arg-type]
                        outbox_drafts.append(
                            OutboxEvent(
                                event_key=self._outbox_event_key(
                                    kind="incident_opened",
                                    metric=metric,
                                    incident_id=stored.id,
                                    previous=previous,
                                    current=current,
                                    eval_identity=eval_key.identity,
                                ),
                                kind="incident_opened",
                                incident_id=stored.id,
                                incident=stored,
                                metric=metric,
                                previous_state=previous,
                                current_state=current,
                                message=f"Opened incident for {metric}",
                                created_at=now,
                            )
                        )
                    else:
                        meaningful = (
                            previous != current
                            or merge_active.explanatory_kpi != draft_incident.explanatory_kpi
                            or merge_active.owner != draft_incident.owner
                            or merge_active.state != IncidentState.OPEN
                        )
                        merged = draft_incident.model_copy(
                            update={
                                "id": merge_active.id,
                                "first_detected": merge_active.first_detected,
                                "opened_at": merge_active.opened_at or draft_incident.opened_at,
                                "state": IncidentState.OPEN,
                            }
                        )
                        stored = tx.stage_incident(merged)
                        staged_incidents.append(stored)
                        updated_incident_ids.append(stored.id)  # type: ignore[arg-type]
                        if meaningful:
                            outbox_drafts.append(
                                OutboxEvent(
                                    event_key=self._outbox_event_key(
                                        kind="incident_updated",
                                        metric=metric,
                                        incident_id=stored.id,
                                        previous=previous,
                                        current=current,
                                        eval_identity=eval_key.identity,
                                    ),
                                    kind="incident_updated",
                                    incident_id=stored.id,
                                    incident=stored,
                                    metric=metric,
                                    previous_state=previous,
                                    current_state=current,
                                    message=f"Updated incident for {metric}",
                                    created_at=now,
                                )
                            )

                elif current == KPIState.RESOLVED and draft_incident is not None:
                    stored = tx.stage_incident(draft_incident)
                    staged_incidents.append(stored)
                    updated_incident_ids.append(stored.id)  # type: ignore[arg-type]
                    if previous != current:
                        outbox_drafts.append(
                            OutboxEvent(
                                event_key=self._outbox_event_key(
                                    kind="incident_resolved",
                                    metric=metric,
                                    incident_id=stored.id,
                                    previous=previous,
                                    current=current,
                                    eval_identity=eval_key.identity,
                                ),
                                kind="incident_resolved",
                                incident_id=stored.id,
                                incident=stored,
                                metric=metric,
                                previous_state=previous,
                                current_state=current,
                                message=f"Resolved incident for {metric}",
                                created_at=now,
                            )
                        )

                elif current == KPIState.SUPPRESSED and previous != current:
                    outbox_drafts.append(
                        OutboxEvent(
                            event_key=self._outbox_event_key(
                                kind="state_changed",
                                metric=metric,
                                incident_id=(draft_incident.id if draft_incident else None),
                                previous=previous,
                                current=current,
                                eval_identity=eval_key.identity,
                            ),
                            kind="state_changed",
                            incident_id=(draft_incident.id if draft_incident else None),
                            incident=draft_incident,
                            metric=metric,
                            previous_state=previous,
                            current_state=current,
                            message=f"Suppressed {metric} due to data quality",
                            created_at=now,
                        )
                    )
                    if draft_incident is not None:
                        stored = tx.stage_incident(draft_incident)
                        staged_incidents.append(stored)
                        updated_incident_ids.append(stored.id)  # type: ignore[arg-type]

                notifications = [tx.stage_notification(ev) for ev in outbox_drafts]
                incident_ids = [i.id for i in staged_incidents if i.id is not None]
                record = EvaluationRecord(
                    key=eval_key,
                    observation=observation,
                    transition=transition,
                    status=status,
                    state_record=state_record,
                    incident_ids=incident_ids,
                    outbox_event_ids=[e.id for e in notifications if e.id],
                    new_incident_ids=new_incident_ids,
                    updated_incident_ids=updated_incident_ids,
                    investigation=investigation,
                    committed_at=now,
                )
                tx.stage_evaluation_record(record)
                tx.commit()

            return ProcessResult(
                metric=metric,
                scope=scope,
                evaluation_key=eval_key,
                at=eval_at,
                status=status,
                transition=transition,
                new_incidents=[
                    i
                    for iid in new_incident_ids
                    if (i := self.state_store.get_incident(iid)) is not None
                ],
                updated_incidents=[
                    i
                    for iid in updated_incident_ids
                    if (i := self.state_store.get_incident(iid)) is not None
                ],
                notifications=notifications,
                investigation=investigation,
                idempotent=False,
                evaluation_record=record,
            )
        finally:
            self.state_store.release_evaluation_claim(eval_key, token=claim.token)

    # Alias used by schedulers / workers.
    tick = process
