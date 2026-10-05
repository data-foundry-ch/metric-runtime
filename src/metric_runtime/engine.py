"""KPI evaluation engine.

Python API first. Configuration files for deployment.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from metric_runtime.catalog import MetricCatalog
from metric_runtime.detectors import DetectorStrategy, SeasonalZScoreDetector
from metric_runtime.detectors.specs import build_detector
from metric_runtime.exceptions import (
    EvaluationInProgressError,
    MetricRuntimeError,
    NoDataError,
    StaleEvaluationError,
    StreamCommitConflict,
    UnknownMetricError,
)
from metric_runtime.identity import EvaluationKey, ensure_utc
from metric_runtime.models import (
    DetectorConfig,
    DrilldownRow,
    EvaluationRecord,
    IncidentState,
    InvestigationResult,
    KPIState,
    KPIStateTransition,
    KPIStatus,
    MeasureRef,
    Metric,
    MetricStateRecord,
    OutboxEvent,
    ProcessResult,
    QualityReport,
    StoredObservation,
)
from metric_runtime.notifications.base import Notifier, NullNotifier
from metric_runtime.notifications.delivery import NotificationPolicy, deliver_pending
from metric_runtime.state import StatePolicy, evolve_state, signals_from_history
from metric_runtime.stores.base import EvaluationClaimStatus
from metric_runtime.stores.memory import InMemoryStateStore


class _CommitOutcome(NamedTuple):
    transition: KPIStateTransition
    record: EvaluationRecord
    new_incident_ids: list[str]
    updated_incident_ids: list[str]
    notifications: list[OutboxEvent]
    investigation: InvestigationResult | None


def _resolve_detector(
    metric: Metric,
    engine_default: DetectorStrategy,
) -> tuple[DetectorStrategy, DetectorConfig]:
    """Return (strategy, config) for a Metric.

    Metric.detector is a serializable DetectorSpec (SeasonalZScore | Threshold).
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
        catalog: MetricCatalog | dict[str, Metric] | list[Metric],
        executor=None,
        state_store=None,
        detector: DetectorStrategy | None = None,
        notifier: Notifier | None = None,
        *,
        connection=None,
        fact_table: str | None = None,
        state_policy: StatePolicy | None = None,
        preferred_leaves: tuple[str, ...] = (),
        batch_registry=None,
        runtime_store=None,
        notification_policy: NotificationPolicy | None = None,
    ):
        if (
            runtime_store is not None
            and state_store is not None
            and runtime_store is not state_store
        ):
            raise MetricRuntimeError("Pass runtime_store= or state_store= (deprecated), not both")
        if isinstance(catalog, MetricCatalog):
            self._catalog = catalog
        elif isinstance(catalog, dict):
            self._catalog = MetricCatalog(catalog)
        else:
            self._catalog = MetricCatalog(list(catalog))

        if connection is not None and executor is None:
            from metric_runtime.adapters.duckdb.executor import DuckDBExecutor

            executor = DuckDBExecutor(connection, fact_table=fact_table)

        self.executor = executor
        self.runtime_store = runtime_store or state_store or InMemoryStateStore()
        self.detector = detector or SeasonalZScoreDetector()
        self.notifier = notifier or NullNotifier()
        self.notification_policy = notification_policy or NotificationPolicy()
        # Bounded re-entries of the ordered section on StreamCommitConflict.
        self.commit_attempts = 3
        self.state_policy = state_policy or StatePolicy()
        self.preferred_leaves = preferred_leaves
        self.batch_registry = batch_registry

        # Convenience attributes used by example quality helpers.
        self.fact_table = getattr(executor, "fact_table", fact_table)
        self.con = getattr(executor, "con", connection)

    @property
    def state_store(self):
        """Back-compat alias for :attr:`runtime_store`."""
        return self.runtime_store

    @state_store.setter
    def state_store(self, value) -> None:
        self.runtime_store = value

    @property
    def catalog(self) -> dict[str, Metric]:
        """Dict-like catalog for back-compat with demo/tests."""
        return self._catalog.as_dict()

    @property
    def catalog_dict(self) -> dict[str, Metric]:
        return self._catalog.as_dict()

    @property
    def kpi_catalog(self) -> MetricCatalog:
        return self._catalog

    @classmethod
    def from_profile(
        cls,
        profile: str,
        *,
        project_config: str | Path | None = None,
        connections_config: str | Path | None = None,
        catalog: MetricCatalog | dict[str, Metric] | list[Metric] | None = None,
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
                "KPIEngine has no executor. Pass executor=... (e.g. from an adapter) "
                "or build via KPIEngine.from_profile(...)."
            )
        return self.executor

    def _get_kpi(self, metric_name: str) -> Metric:
        try:
            return self._catalog.get(metric_name)
        except UnknownMetricError:
            raise

    def new_evaluation_session(self):
        """Create a fresh EvaluationSession (session-local batch cache)."""
        from metric_runtime.calculations.batch import BatchRegistry
        from metric_runtime.calculations.session import EvaluationSession

        return EvaluationSession(
            self._catalog,
            executor=self.executor,
            batch_registry=self.batch_registry or BatchRegistry(),
            sql_dialects=getattr(self.executor, "sql_dialects", None),
        )

    @property
    def evaluation_session(self):
        """Compatibility alias — prefer ``new_evaluation_session()`` for isolation."""
        return self.new_evaluation_session()

    def metric_value(
        self,
        metric_name: str,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        session=None,
    ) -> float:
        from metric_runtime.calculations.context import ObservationValueStatus
        from metric_runtime.calculations.session import build_evaluation_context

        context = build_evaluation_context(at=at, start=start, end=end, filters=filters)
        eval_session = session or self.new_evaluation_session()
        result = eval_session.calculate_value(metric_name, context)
        if result.status == ObservationValueStatus.NO_DATA or (
            result.status == ObservationValueStatus.VALUE and result.value is None
        ):
            raise NoDataError(
                f"KPI {metric_name!r} produced {ObservationValueStatus.NO_DATA.value}"
                + (f": {result.error}" if result.error else " (no data)")
            )
        if result.status != ObservationValueStatus.VALUE or result.value is None:
            raise MetricRuntimeError(
                f"KPI {metric_name!r} produced {result.status.value}"
                + (f": {result.error}" if result.error else " (no data)")
            )
        return float(result.value)

    def _usable_baseline(self, metric_name: str, windows: list[dict]) -> list[float]:
        """Baseline values, skipping windows that produced NO_DATA.

        Raises :class:`NoDataError` (reason ``no_baseline``) when baseline
        windows were requested but none produced a value.
        """
        values: list[float] = []
        for kwargs in windows:
            try:
                values.append(self.metric_value(metric_name, **kwargs))
            except NoDataError:
                continue
        if windows and not values:
            raise NoDataError(
                f"KPI {metric_name!r} has no usable baseline windows",
                reason="no_baseline",
            )
        return values

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
        *,
        session=None,
    ) -> list[float]:
        metric = self._get_kpi(metric_name)
        _, cfg = _resolve_detector(metric, self.detector)
        return [
            self.metric_value(metric_name, at - timedelta(weeks=i), filters, session=session)
            for i in range(1, cfg.baseline_weeks + 1)
        ]

    def evaluate(
        self,
        metric_name: str,
        at: datetime,
        filters: dict[str, str] | None = None,
        *,
        session=None,
    ) -> KPIStatus:
        at = ensure_utc(at)
        metric = self._get_kpi(metric_name)
        strategy, cfg = _resolve_detector(metric, self.detector)
        current = self.metric_value(metric_name, at, filters, session=session)
        baseline = self._usable_baseline(
            metric_name,
            [
                {"at": at - timedelta(weeks=i), "filters": filters, "session": session}
                for i in range(1, cfg.baseline_weeks + 1)
            ],
        )
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
        baseline = self._usable_baseline(
            metric_name,
            [
                {
                    "filters": filters,
                    "start": start - timedelta(weeks=i),
                    "end": end - timedelta(weeks=i),
                }
                for i in range(1, weeks + 1)
            ],
        )
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

    def estimate_impact(
        self,
        metric_name: str,
        at: datetime,
        current_value: float,
        baseline_value: float,
        filters: dict[str, str] | None = None,
    ) -> float:
        """Estimate absolute business impact (currency-agnostic magnitude).

        Delegates to :meth:`estimate_impact_eur` so existing monkeypatches keep
        working during the rename transition.
        """
        return self.estimate_impact_eur(metric_name, at, current_value, baseline_value, filters)

    def estimate_impact_eur(
        self,
        metric_name: str,
        at: datetime,
        current_value: float,
        baseline_value: float,
        filters: dict[str, str] | None = None,
    ) -> float:
        """Impact estimator (name retained for back-compat; currency-agnostic)."""
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
            impact = self.estimate_impact_eur(
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
                    impact=impact,
                )
            )
        rows.sort(key=lambda row: row.impact, reverse=True)
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
                last_evaluation_at=previous.last_evaluation_at,
                version=previous.version + 1,
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
        """Delivery worker: send due outbox events through the notifier.

        Thin wrapper over :func:`~metric_runtime.notifications.delivery.deliver_pending`
        using ``self.notification_policy``. External delivery is at-least-once.
        Failures are recorded on the event and do not block later events.
        Returns events successfully marked delivered in this pass.
        """
        report = deliver_pending(
            self.runtime_store,
            self.notifier,
            policy=self.notification_policy,
            limit=limit,
        )
        return report.delivered

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
            processed_at=record.committed_at,
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
        session=None,
    ) -> ProcessResult:
        """Authoritative runtime tick with atomic, ordered commit semantics.

        Calculate once per EvaluationKey. Apply metric-state transitions in
        ``effective_at`` order per (metric, scope). Commit once.

        Pass a shared ``session`` (see :meth:`process_many`) so BatchCalculation
        sources execute once across related metrics.

        A current window that produces NO_DATA is committed as a NO_DATA
        evaluation (state carried forward, no incidents/outbox). Calculation
        errors raise and commit nothing, so the window can be retried.
        """
        scope = dict(scope or {})
        leaves = preferred_leaves or self.preferred_leaves
        eval_key = EvaluationKey.build(metric, scope=scope, at=at, start=start, end=end)
        eval_at = eval_key.eval_at

        claim = self.runtime_store.claim_evaluation(eval_key)
        if claim.status == EvaluationClaimStatus.ALREADY_COMMITTED:
            assert claim.record is not None
            return self._result_from_record(claim.record)
        if claim.status == EvaluationClaimStatus.IN_PROGRESS:
            raise EvaluationInProgressError(
                f"Evaluation already in progress for {eval_key.identity}"
            )

        try:
            no_data_reason: str | None = None
            try:
                if start is not None and end is not None:
                    status = self.evaluate_window(metric, start, end, scope)
                else:
                    assert at is not None
                    if session is None:
                        status = self.evaluate(metric, at, scope)
                    else:
                        status = self.evaluate(metric, at, scope, session=session)
            except NoDataError as exc:
                no_data_reason = exc.reason
                status = self._no_data_status(metric, eval_at)

            quality_ok = True if quality is None else quality.healthy
            now = datetime.now(UTC)
            observation = StoredObservation(
                key=eval_key,
                status=status,
                eligible_for_state=quality_ok and no_data_reason is None,
                quality_healthy=None if quality is None else quality.healthy,
                recorded_at=now,
                value_status="value" if no_data_reason is None else "no_data",
                no_data_reason=no_data_reason,
            )

            attempts = 0
            while True:
                try:
                    outcome = self._commit_ordered(
                        eval_key,
                        claim_token=claim.token,
                        observation=observation,
                        status=status,
                        scope=scope,
                        quality=quality,
                        leaves=leaves,
                        context=context,
                        now=now,
                    )
                    break
                except StreamCommitConflict:
                    attempts += 1
                    if attempts >= max(1, self.commit_attempts):
                        raise

            store = self.runtime_store
            return ProcessResult(
                metric=metric,
                scope=scope,
                evaluation_key=eval_key,
                at=eval_at,
                status=status,
                transition=outcome.transition,
                new_incidents=[
                    i
                    for iid in outcome.new_incident_ids
                    if (i := store.get_incident(iid)) is not None
                ],
                updated_incidents=[
                    i
                    for iid in outcome.updated_incident_ids
                    if (i := store.get_incident(iid)) is not None
                ],
                notifications=outcome.notifications,
                investigation=outcome.investigation,
                idempotent=False,
                evaluation_record=outcome.record,
                processed_at=now,
            )
        finally:
            self.runtime_store.release_evaluation_claim(eval_key, token=claim.token)

    def _no_data_status(self, metric_name: str, as_of: datetime) -> KPIStatus:
        metric = self._get_kpi(metric_name)
        return KPIStatus(
            name=metric_name,
            value=0.0,
            baseline_mean=0.0,
            baseline_std=0.0,
            z_score=0.0,
            relative_change=0.0,
            anomaly=False,
            support=0.0,
            support_ok=False,
            as_of=as_of,
            directionality=metric.directionality,
            value_status="no_data",
        )

    def _commit_ordered(
        self,
        eval_key: EvaluationKey,
        *,
        claim_token: str | None,
        observation: StoredObservation,
        status: KPIStatus,
        scope: dict[str, str],
        quality: QualityReport | None,
        leaves: tuple[str, ...],
        context: list[str] | None,
        now: datetime,
    ) -> _CommitOutcome:
        """Ordered section: read state, compute transition, stage and commit once.

        Re-entered by :meth:`process` on :class:`StreamCommitConflict`.
        """
        from metric_runtime.incidents import incident_from_investigation
        from metric_runtime.state import (
            _trailing_detection_streak,
            _trailing_healthy_streak,
        )

        store = self.runtime_store
        policy = self.state_policy
        metric = eval_key.metric
        scope_key = eval_key.scope_key
        eval_at = eval_key.eval_at

        # Warehouse work may finish out of order; metric-state application
        # is serialized by effective_at for this (metric, scope) stream.
        with store.ordered_stream_commit(eval_key):
            previous_record = store.get_state_record(metric, scope_key)
            if (
                previous_record.last_evaluation_at is not None
                and ensure_utc(previous_record.last_evaluation_at) >= eval_at
            ):
                raise StaleEvaluationError(
                    f"Stale evaluation for {metric}: "
                    f"effective_at={eval_at.isoformat()} is not after "
                    f"last_evaluation_at="
                    f"{ensure_utc(previous_record.last_evaluation_at).isoformat()}"
                )

            previous = previous_record.state
            investigation = None
            draft_incident = None
            merge_active = None
            outbox_drafts: list[OutboxEvent] = []

            if observation.is_no_data:
                # NO_DATA is a fact about this window, not a signal: carry the
                # state forward unchanged and only advance version/cursor.
                current = previous
                state_record = previous_record.model_copy(
                    update={
                        "scope": scope or previous_record.scope,
                        "updated_at": now,
                        "last_evaluation_at": eval_at,
                        "version": previous_record.version + 1,
                    }
                )
            else:
                history = list(store.get_history(metric, scope_key)) + [observation]
                signals = signals_from_history(history)
                impact = self.estimate_impact_eur(
                    metric, eval_at, status.value, status.baseline_mean, scope
                )
                current = evolve_state(
                    signals,
                    policy=policy,
                    impact=impact,
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
                    detection_streak=_trailing_detection_streak(signals),
                    healthy_streak=_trailing_healthy_streak(signals),
                    last_evaluation_at=eval_at,
                    version=previous_record.version + 1,
                )

                active = store.find_active_incident(metric, scope_key)
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
                        update={
                            "state": IncidentState.SUPPRESSED,
                            "updated_at": eval_at,
                        }
                    )
                    merge_active = active

            transition = KPIStateTransition(previous=previous, current=current)

            with store.transaction(evaluation_key=eval_key, claim_token=claim_token) as tx:
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

                elif (
                    current == KPIState.SUPPRESSED
                    and previous != current
                    and draft_incident is not None
                ):
                    # Notifier protocol requires an Incident — only enqueue
                    # when an active incident is being suppressed.
                    stored = tx.stage_incident(draft_incident)
                    staged_incidents.append(stored)
                    updated_incident_ids.append(stored.id)  # type: ignore[arg-type]
                    outbox_drafts.append(
                        OutboxEvent(
                            event_key=self._outbox_event_key(
                                kind="state_changed",
                                metric=metric,
                                incident_id=stored.id,
                                previous=previous,
                                current=current,
                                eval_identity=eval_key.identity,
                            ),
                            kind="state_changed",
                            incident_id=stored.id,
                            incident=stored,
                            metric=metric,
                            previous_state=previous,
                            current_state=current,
                            message=f"Suppressed {metric} due to data quality",
                            created_at=now,
                        )
                    )

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
                    effective_at=eval_at,
                )
                tx.stage_evaluation_record(record)
                tx.commit()

        return _CommitOutcome(
            transition=transition,
            record=record,
            new_incident_ids=new_incident_ids,
            updated_incident_ids=updated_incident_ids,
            notifications=notifications,
            investigation=investigation,
        )

    def process_many(
        self,
        metrics: Iterable[str],
        *,
        at: datetime | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        scope: dict[str, str] | None = None,
        quality: QualityReport | None = None,
        preferred_leaves: tuple[str, ...] = (),
        context: list[str] | None = None,
    ) -> dict[str, ProcessResult]:
        """Process several metrics sharing one EvaluationSession batch cache.

        Prefer this over repeated :meth:`process` calls when metrics share
        ``BatchCalculation`` sources for the same evaluation context.
        """
        from metric_runtime.calculations.session import build_evaluation_context

        metric_ids = [str(m) for m in metrics]
        session = self.new_evaluation_session()
        # Warm the session-local batch cache for the primary evaluation window.
        if at is not None or (start is not None and end is not None):
            ctx = build_evaluation_context(at=at, start=start, end=end, filters=scope)
            session.evaluate_all(metric_ids, ctx)

        return {
            metric_id: self.process(
                metric_id,
                at=at,
                start=start,
                end=end,
                scope=scope,
                quality=quality,
                preferred_leaves=preferred_leaves,
                context=context,
                session=session,
            )
            for metric_id in metric_ids
        }

    # Alias used by schedulers / workers.
    tick = process
