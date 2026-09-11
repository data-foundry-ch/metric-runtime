"""In-memory transactional runtime store — zero infrastructure."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from metric_runtime.exceptions import (
    EvaluationInProgressError,
    MetricRuntimeError,
    StaleEvaluationError,
)
from metric_runtime.identity import EvaluationKey, canonical_scope_key, ensure_utc
from metric_runtime.models import (
    EvaluationRecord,
    Incident,
    IncidentState,
    KPIState,
    KPIStatus,
    MetricStateRecord,
    OutboxEvent,
    StoredObservation,
)
from metric_runtime.stores.base import EvaluationClaim, EvaluationClaimStatus

_ACTIVE_INCIDENT_STATES = {
    IncidentState.OPEN,
    IncidentState.DETECTED,
    IncidentState.ACKNOWLEDGED,
}


class _InMemoryTransaction:
    """Stages mutations; commit applies them atomically under the store lock."""

    def __init__(self, store: InMemoryStateStore) -> None:
        self._store = store
        self._obs: dict[str, StoredObservation] = {}
        self._states: dict[tuple[str, str], MetricStateRecord] = {}
        self._incidents: dict[str, Incident] = {}
        self._outbox: dict[str, OutboxEvent] = {}
        self._evaluations: dict[str, EvaluationRecord] = {}
        self._history_adds: dict[tuple[str, str], list[str]] = {}
        self._committed = False
        self._rolled_back = False
        self._incident_seq_offset = 0
        self._outbox_seq_offset = 0

    def get_observation(self, key: EvaluationKey) -> StoredObservation | None:
        obs_id = InMemoryStateStore._obs_id(key)
        if obs_id in self._obs:
            return self._obs[obs_id]
        return self._store.get_observation(key)

    def get_committed_result(self, key: EvaluationKey) -> EvaluationRecord | None:
        identity = key.identity
        if identity in self._evaluations:
            return self._evaluations[identity]
        return self._store.get_committed_result(key)

    def get_history(self, metric: str, scope_key: str = "") -> list[StoredObservation]:
        base = self._store.get_history(metric, scope_key)
        by_id = {InMemoryStateStore._obs_id(item.key): item for item in base}
        for obs_id, obs in self._obs.items():
            if obs.key.metric == metric and obs.key.scope_key == scope_key:
                by_id[obs_id] = obs
        order = list(self._store._history.get((metric, scope_key), []))
        for obs_id in self._history_adds.get((metric, scope_key), []):
            if obs_id not in order:
                order.append(obs_id)
        return [by_id[obs_id] for obs_id in order if obs_id in by_id]

    def get_state_record(self, metric: str, scope_key: str = "") -> MetricStateRecord:
        staged = self._states.get((metric, scope_key))
        if staged is not None:
            return staged
        return self._store.get_state_record(metric, scope_key)

    def find_active_incident(self, metric: str, scope_key: str = "") -> Incident | None:
        committed = {i.id: i for i in self._store.list_open_incidents() if i.id is not None}
        for incident in self._incidents.values():
            if incident.id is not None:
                committed[incident.id] = incident
        matches = [
            i
            for i in committed.values()
            if i.primary_metric == metric
            and canonical_scope_key(i.scope) == scope_key
            and i.state in _ACTIVE_INCIDENT_STATES
        ]
        if not matches:
            return None

        def _sort_key(i: Incident) -> str:
            stamp = i.updated_at or i.opened_at or i.first_detected
            return ensure_utc(stamp).isoformat()

        matches.sort(key=_sort_key)
        return matches[-1]

    def get_incident(self, incident_id: str) -> Incident | None:
        if incident_id in self._incidents:
            return self._incidents[incident_id]
        return self._store.get_incident(incident_id)

    def stage_observation(self, observation: StoredObservation) -> None:
        self._ensure_open()
        obs_id = InMemoryStateStore._obs_id(observation.key)
        self._obs[obs_id] = observation
        hist_key = (observation.key.metric, observation.key.scope_key)
        self._history_adds.setdefault(hist_key, []).append(obs_id)

    def stage_state_record(self, record: MetricStateRecord) -> None:
        self._ensure_open()
        self._states[(record.metric, record.scope_key)] = record

    def stage_incident(self, incident: Incident) -> Incident:
        self._ensure_open()
        if not incident.id:
            self._incident_seq_offset += 1
            next_id = self._store._incident_seq + self._incident_seq_offset
            incident = incident.model_copy(update={"id": f"inc-{next_id:04d}"})
        assert incident.id is not None
        self._incidents[incident.id] = incident
        return incident

    def stage_notification(self, event: OutboxEvent) -> OutboxEvent:
        self._ensure_open()
        if event.event_key:
            staged = next(
                (e for e in self._outbox.values() if e.event_key == event.event_key),
                None,
            )
            if staged is not None:
                return staged
            existing_id = self._store._outbox_by_key.get(event.event_key)
            if existing_id is not None:
                existing = self._store._outbox.get(existing_id)
                if existing is not None:
                    return existing
        if not event.id:
            self._outbox_seq_offset += 1
            next_id = self._store._outbox_seq + self._outbox_seq_offset
            event = event.model_copy(update={"id": f"out-{next_id:04d}"})
        assert event.id is not None
        self._outbox[event.id] = event
        return event

    def stage_evaluation_record(self, record: EvaluationRecord) -> None:
        self._ensure_open()
        self._evaluations[record.key.identity] = record

    def commit(self) -> None:
        self._ensure_open()
        with self._store._lock:
            if callable(self._store.commit_hook):
                self._store.commit_hook(self)
            for obs_id, obs in self._obs.items():
                self._store._observations[obs_id] = obs
                hist_key = (obs.key.metric, obs.key.scope_key)
                ids = self._store._history.setdefault(hist_key, [])
                if obs_id not in ids:
                    ids.append(obs_id)
            for state_key, state_record in self._states.items():
                existing = self._store._states.get(state_key)
                if existing is not None and state_record.version != existing.version + 1:
                    raise StaleEvaluationError(
                        f"Optimistic version conflict for {state_key[0]}/"
                        f"{state_key[1]}: expected version {existing.version + 1}, "
                        f"got {state_record.version}"
                    )
                self._store._states[state_key] = state_record
            for incident_id, incident in self._incidents.items():
                self._store._incidents[incident_id] = incident
            self._store._incident_seq += self._incident_seq_offset
            for event_id, event in self._outbox.items():
                self._store._outbox[event_id] = event
                if event.event_key:
                    self._store._outbox_by_key[event.event_key] = event_id
            self._store._outbox_seq += self._outbox_seq_offset
            for identity, evaluation in self._evaluations.items():
                self._store._evaluations[identity] = evaluation
            self._committed = True

    def rollback(self) -> None:
        self._rolled_back = True
        self._obs.clear()
        self._states.clear()
        self._incidents.clear()
        self._outbox.clear()
        self._evaluations.clear()
        self._history_adds.clear()

    def _ensure_open(self) -> None:
        if self._committed or self._rolled_back:
            raise MetricRuntimeError("Transaction is already closed")


class InMemoryStateStore:
    """Process-local transactional observation + state + incident + outbox store."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[tuple[str, str], MetricStateRecord] = {}
        self._observations: dict[str, StoredObservation] = {}
        self._history: dict[tuple[str, str], list[str]] = {}
        self._incidents: dict[str, Incident] = {}
        self._outbox: dict[str, OutboxEvent] = {}
        self._outbox_by_key: dict[str, str] = {}
        self._evaluations: dict[str, EvaluationRecord] = {}
        self._claims: dict[str, str] = {}
        # identity -> effective_at for in-flight evaluations per stream.
        self._stream_inflight: dict[tuple[str, str], dict[str, datetime]] = {}
        self._stream_busy: dict[tuple[str, str], str] = {}
        self._stream_conditions: dict[tuple[str, str], threading.Condition] = {}
        self._incident_seq = 0
        self._outbox_seq = 0
        # Optional hook for failure-injection tests: called inside commit.
        self.commit_hook = None

    @staticmethod
    def _obs_id(key: EvaluationKey) -> str:
        return key.identity

    def get_observation(self, key: EvaluationKey) -> StoredObservation | None:
        return self._observations.get(self._obs_id(key))

    def put_observation(self, observation: StoredObservation) -> StoredObservation:
        with self.transaction() as tx:
            tx.stage_observation(observation)
            tx.commit()
        return observation

    def get_history(self, metric: str, scope_key: str = "") -> list[StoredObservation]:
        return [
            self._observations[obs_id]
            for obs_id in self._history.get((metric, scope_key), [])
            if obs_id in self._observations
        ]

    def append_observation(self, metric: str, status: KPIStatus, scope_key: str = "") -> None:
        import warnings

        warnings.warn(
            "append_observation is deprecated; use process()/transaction staging",
            DeprecationWarning,
            stacklevel=2,
        )
        key = EvaluationKey.build(metric, scope={}, at=status.as_of)
        object.__setattr__(key, "scope_key", scope_key)
        self.put_observation(
            StoredObservation(
                key=key,
                status=status,
                eligible_for_state=True,
                recorded_at=datetime.now(UTC),
            )
        )

    def has_observation(self, metric: str, as_of: str, scope_key: str = "") -> bool:
        import warnings

        warnings.warn(
            "has_observation is deprecated; use get_observation(EvaluationKey)",
            DeprecationWarning,
            stacklevel=2,
        )
        for item in self.get_history(metric, scope_key):
            if item.key.window_id == as_of or item.status.as_of.isoformat() == as_of:
                return True
        return False

    def get_state_record(self, metric: str, scope_key: str = "") -> MetricStateRecord:
        existing = self._states.get((metric, scope_key))
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        return MetricStateRecord(
            metric=metric,
            scope_key=scope_key,
            state=KPIState.NORMAL,
            state_since=now,
            updated_at=now,
            last_evaluation_at=None,
            version=0,
        )

    def set_state_record(self, record: MetricStateRecord) -> None:
        with self.transaction() as tx:
            tx.stage_state_record(record)
            tx.commit()

    def get_state(self, metric: str, scope_key: str = "") -> KPIState:
        """Convenience read — prefer ``get_state_record`` for durable fields."""
        return self.get_state_record(metric, scope_key).state

    def set_state(self, metric: str, state: KPIState, scope_key: str = "") -> None:
        import warnings

        warnings.warn(
            "set_state is deprecated; mutate MetricStateRecord via process()/set_state_record",
            DeprecationWarning,
            stacklevel=2,
        )
        now = datetime.now(UTC)
        previous = self._states.get((metric, scope_key))
        resolved_at = previous.resolved_at if previous is not None else None
        opened_at = previous.opened_at if previous is not None else None
        acknowledged_at = previous.acknowledged_at if previous is not None else None
        state_since = previous.state_since if previous is not None else now
        last_evaluation_at = previous.last_evaluation_at if previous is not None else None
        version = previous.version if previous is not None else 0
        if previous is None or previous.state != state:
            state_since = now
        if state == KPIState.RESOLVED:
            resolved_at = now
        if state == KPIState.OPEN:
            opened_at = opened_at or now
        if state == KPIState.ACKNOWLEDGED:
            acknowledged_at = now
        self.set_state_record(
            MetricStateRecord(
                metric=metric,
                scope_key=scope_key,
                scope=previous.scope if previous is not None else {},
                state=state,
                state_since=state_since,
                updated_at=now,
                opened_at=opened_at,
                acknowledged_at=acknowledged_at,
                resolved_at=resolved_at,
                last_evaluation_at=last_evaluation_at,
                version=version + 1,
            )
        )

    def get_incident(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    def upsert_incident(self, incident: Incident) -> Incident:
        with self.transaction() as tx:
            stored = tx.stage_incident(incident)
            tx.commit()
        return stored

    def list_open_incidents(self) -> list[Incident]:
        return [i for i in self._incidents.values() if i.state in _ACTIVE_INCIDENT_STATES]

    def find_active_incident(self, metric: str, scope_key: str = "") -> Incident | None:
        matches = [
            i
            for i in self.list_open_incidents()
            if i.primary_metric == metric and canonical_scope_key(i.scope) == scope_key
        ]
        if not matches:
            return None

        def _sort_key(i: Incident) -> str:
            stamp = i.updated_at or i.opened_at or i.first_detected
            return ensure_utc(stamp).isoformat()

        matches.sort(key=_sort_key)
        return matches[-1]

    def enqueue_notification(self, event: OutboxEvent) -> OutboxEvent:
        with self.transaction() as tx:
            stored = tx.stage_notification(event)
            tx.commit()
        return stored

    def list_pending_notifications(self) -> list[OutboxEvent]:
        pending = [e for e in self._outbox.values() if e.delivered_at is None]
        pending.sort(key=lambda e: ensure_utc(e.created_at).isoformat())
        return pending

    def mark_notification_delivered(self, event_id: str, *, at: datetime) -> OutboxEvent:
        with self._lock:
            event = self._outbox[event_id]
            updated = event.model_copy(
                update={
                    "delivered_at": ensure_utc(at),
                    "last_attempt_at": ensure_utc(at),
                    "attempt_count": event.attempt_count + 1,
                    "last_error": None,
                }
            )
            self._outbox[event_id] = updated
            return updated

    def record_notification_attempt(
        self,
        event_id: str,
        *,
        at: datetime,
        error: str | None = None,
    ) -> OutboxEvent:
        with self._lock:
            event = self._outbox[event_id]
            updated = event.model_copy(
                update={
                    "last_attempt_at": ensure_utc(at),
                    "attempt_count": event.attempt_count + 1,
                    "last_error": error,
                }
            )
            self._outbox[event_id] = updated
            return updated

    def get_committed_result(self, key: EvaluationKey) -> EvaluationRecord | None:
        return self._evaluations.get(key.identity)

    def _stream_id(self, key: EvaluationKey) -> tuple[str, str]:
        return (key.metric, key.scope_key)

    def _stream_condition(self, stream: tuple[str, str]) -> threading.Condition:
        cond = self._stream_conditions.get(stream)
        if cond is None:
            cond = threading.Condition(self._lock)
            self._stream_conditions[stream] = cond
        return cond

    def claim_evaluation(self, key: EvaluationKey) -> EvaluationClaim:
        with self._lock:
            existing = self._evaluations.get(key.identity)
            if existing is not None:
                return EvaluationClaim(
                    EvaluationClaimStatus.ALREADY_COMMITTED,
                    key=key,
                    record=existing,
                )
            if key.identity in self._claims:
                return EvaluationClaim(EvaluationClaimStatus.IN_PROGRESS, key=key)
            token = uuid.uuid4().hex
            self._claims[key.identity] = token
            stream = self._stream_id(key)
            self._stream_inflight.setdefault(stream, {})[key.identity] = ensure_utc(key.eval_at)
            self._stream_condition(stream).notify_all()
            return EvaluationClaim(
                EvaluationClaimStatus.ACQUIRED,
                key=key,
                token=token,
            )

    def release_evaluation_claim(self, key: EvaluationKey, *, token: str | None = None) -> None:
        with self._lock:
            current = self._claims.get(key.identity)
            if current is None:
                return
            if token is not None and current != token:
                return
            del self._claims[key.identity]
            stream = self._stream_id(key)
            inflight = self._stream_inflight.get(stream)
            if inflight is not None:
                inflight.pop(key.identity, None)
                if not inflight:
                    self._stream_inflight.pop(stream, None)
            self._stream_condition(stream).notify_all()

    @contextmanager
    def ordered_stream_commit(
        self,
        key: EvaluationKey,
        *,
        timeout: float | None = 30.0,
    ) -> Iterator[None]:
        """Serialize metric-state commits by effective_at for one stream."""
        stream = self._stream_id(key)
        effective = ensure_utc(key.eval_at)
        cond = self._stream_condition(stream)
        with cond:
            while True:
                existing = self._states.get(stream)
                last = (
                    ensure_utc(existing.last_evaluation_at)
                    if existing is not None and existing.last_evaluation_at is not None
                    else None
                )
                if last is not None and last >= effective:
                    raise StaleEvaluationError(
                        f"Stale evaluation for {key.metric} scope={key.scope_key}: "
                        f"effective_at={effective.isoformat()} is not after "
                        f"last_evaluation_at={last.isoformat()}"
                    )
                earlier = [
                    ident
                    for ident, at in self._stream_inflight.get(stream, {}).items()
                    if at < effective and ident != key.identity
                ]
                busy = self._stream_busy.get(stream)
                if not earlier and busy is None:
                    self._stream_busy[stream] = key.identity
                    break
                if not cond.wait(timeout=timeout):
                    raise EvaluationInProgressError(
                        f"Timed out waiting for earlier evaluations on "
                        f"{key.metric}/{key.scope_key} before {effective.isoformat()}"
                    )
        try:
            yield
        finally:
            with cond:
                if self._stream_busy.get(stream) == key.identity:
                    del self._stream_busy[stream]
                cond.notify_all()

    @contextmanager
    def transaction(self) -> Iterator[_InMemoryTransaction]:
        tx = _InMemoryTransaction(self)
        try:
            yield tx
            if not tx._committed and not tx._rolled_back:
                # Auto-rollback if caller forgot commit.
                tx.rollback()
        except Exception:
            if not tx._committed:
                tx.rollback()
            raise

    def commit_transaction(self, tx: _InMemoryTransaction) -> None:
        """Deprecated helper — use ``tx.commit()``."""
        import warnings

        warnings.warn(
            "commit_transaction is deprecated; call tx.commit() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        tx.commit()
