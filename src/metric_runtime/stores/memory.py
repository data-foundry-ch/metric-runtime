"""In-memory transactional runtime store — zero infrastructure."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from metric_runtime.exceptions import (
    EvaluationInProgressError,
    NotificationLeaseLostError,
    StaleEvaluationError,
)
from metric_runtime.identity import EvaluationKey, canonical_scope_key, ensure_utc
from metric_runtime.models import (
    EvaluationRecord,
    Incident,
    KPIState,
    KPIStatus,
    MetricStateRecord,
    OutboxEvent,
    StoredObservation,
)
from metric_runtime.stores.base import EvaluationClaim, EvaluationClaimStatus
from metric_runtime.stores.staging import (
    ACTIVE_INCIDENT_STATES,
    StagedTransaction,
    latest_incident,
)


class _InMemoryTransaction(StagedTransaction):
    """Stages mutations; commit applies them atomically under the store lock."""

    def __init__(
        self,
        store: InMemoryStateStore,
        *,
        evaluation_key: EvaluationKey | None = None,
        claim_token: str | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._evaluation_key = evaluation_key
        self._claim_token = claim_token
        self._incident_seq_offset = 0
        self._outbox_seq_offset = 0

    def _committed_observation(self, key: EvaluationKey) -> StoredObservation | None:
        return self._store.get_observation(key)

    def _committed_result(self, key: EvaluationKey) -> EvaluationRecord | None:
        return self._store.get_committed_result(key)

    def _committed_history(self, metric: str, scope_key: str) -> list[StoredObservation]:
        return self._store.get_history(metric, scope_key)

    def _committed_state(self, metric: str, scope_key: str) -> MetricStateRecord:
        return self._store.get_state_record(metric, scope_key)

    def _committed_active_incidents(self, metric: str, scope_key: str) -> list[Incident]:
        return self._store.list_open_incidents()

    def _committed_incident(self, incident_id: str) -> Incident | None:
        return self._store.get_incident(incident_id)

    def _committed_notification_by_key(self, event_key: str) -> OutboxEvent | None:
        existing_id = self._store._outbox_by_key.get(event_key)
        if existing_id is None:
            return None
        return self._store._outbox.get(existing_id)

    def _allocate_incident_id(self) -> str:
        self._incident_seq_offset += 1
        return f"inc-{self._store._incident_seq + self._incident_seq_offset:04d}"

    def _allocate_outbox_id(self) -> str:
        self._outbox_seq_offset += 1
        return f"out-{self._store._outbox_seq + self._outbox_seq_offset:04d}"

    def _apply(self) -> None:
        store = self._store
        with store._lock:
            if callable(store.commit_hook):
                store.commit_hook(self)
            if self._evaluation_key is not None and self._claim_token is not None:
                current = store._claims.get(self._evaluation_key.identity)
                if current != self._claim_token:
                    raise EvaluationInProgressError(
                        f"Evaluation claim for {self._evaluation_key.identity} was lost "
                        "before commit"
                    )
            for obs_id, obs in self._obs.items():
                store._observations[obs_id] = obs
                hist_key = (obs.key.metric, obs.key.scope_key)
                ids = store._history.setdefault(hist_key, [])
                if obs_id not in ids:
                    ids.append(obs_id)
            for state_key, state_record in self._states.items():
                existing = store._states.get(state_key)
                if existing is not None and state_record.version != existing.version + 1:
                    raise StaleEvaluationError(
                        f"Optimistic version conflict for {state_key[0]}/"
                        f"{state_key[1]}: expected version {existing.version + 1}, "
                        f"got {state_record.version}"
                    )
                store._states[state_key] = state_record
            for incident_id, incident in self._incidents.items():
                store._incidents[incident_id] = incident
            store._incident_seq += self._incident_seq_offset
            for event_id, event in self._outbox.items():
                store._outbox[event_id] = event
                if event.event_key:
                    store._outbox_by_key[event.event_key] = event_id
            store._outbox_seq += self._outbox_seq_offset
            for identity, evaluation in self._evaluations.items():
                store._evaluations[identity] = evaluation


class InMemoryStateStore:
    """Process-local transactional observation + state + incident + outbox store.

    ``claim_ttl`` (seconds) optionally lets an expired evaluation claim be
    taken over; the default ``None`` keeps claims until released.
    """

    def __init__(self, *, claim_ttl: float | None = None) -> None:
        self._lock = threading.RLock()
        self._states: dict[tuple[str, str], MetricStateRecord] = {}
        self._observations: dict[str, StoredObservation] = {}
        self._history: dict[tuple[str, str], list[str]] = {}
        self._incidents: dict[str, Incident] = {}
        self._outbox: dict[str, OutboxEvent] = {}
        self._outbox_by_key: dict[str, str] = {}
        self._evaluations: dict[str, EvaluationRecord] = {}
        self._claims: dict[str, str] = {}
        self._claim_expiry: dict[str, datetime] = {}
        self.claim_ttl = claim_ttl
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

    def close(self) -> None:
        return None

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
        return [i for i in self._incidents.values() if i.state in ACTIVE_INCIDENT_STATES]

    def find_active_incident(self, metric: str, scope_key: str = "") -> Incident | None:
        return latest_incident(
            [
                i
                for i in self.list_open_incidents()
                if i.primary_metric == metric and canonical_scope_key(i.scope) == scope_key
            ]
        )

    # --- notification outbox -------------------------------------------------------

    def enqueue_notification(self, event: OutboxEvent) -> OutboxEvent:
        with self.transaction() as tx:
            stored = tx.stage_notification(event)
            tx.commit()
        return stored

    def list_pending_notifications(self) -> list[OutboxEvent]:
        pending = [e for e in self._outbox.values() if e.pending]
        pending.sort(key=lambda e: ensure_utc(e.created_at).isoformat())
        return pending

    def claim_pending_notifications(
        self,
        *,
        now: datetime,
        limit: int | None = None,
        lease: timedelta,
    ) -> list[OutboxEvent]:
        now = ensure_utc(now)
        with self._lock:
            due = [
                e
                for e in self.list_pending_notifications()
                if (e.next_attempt_at is None or ensure_utc(e.next_attempt_at) <= now)
                and (e.claimed_until is None or ensure_utc(e.claimed_until) <= now)
            ]
            if limit is not None:
                due = due[:limit]
            claimed: list[OutboxEvent] = []
            for event in due:
                assert event.id is not None
                updated = event.model_copy(
                    update={
                        "claim_token": uuid.uuid4().hex,
                        "claimed_until": now + lease,
                    }
                )
                self._outbox[event.id] = updated
                claimed.append(updated)
            return claimed

    def _require_lease(self, event: OutboxEvent, claim_token: str | None) -> None:
        if claim_token is not None and event.claim_token != claim_token:
            raise NotificationLeaseLostError(
                f"Outbox event {event.id} lease is no longer held by this worker"
            )

    def mark_notification_delivered(
        self,
        event_id: str,
        *,
        at: datetime,
        claim_token: str | None = None,
    ) -> OutboxEvent:
        with self._lock:
            event = self._outbox[event_id]
            self._require_lease(event, claim_token)
            updated = event.model_copy(
                update={
                    "delivered_at": ensure_utc(at),
                    "last_attempt_at": ensure_utc(at),
                    "attempt_count": event.attempt_count + 1,
                    "last_error": None,
                    "claim_token": None,
                    "claimed_until": None,
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
        claim_token: str | None = None,
        next_attempt_at: datetime | None = None,
        dead_letter: bool = False,
    ) -> OutboxEvent:
        with self._lock:
            event = self._outbox[event_id]
            self._require_lease(event, claim_token)
            updated = event.model_copy(
                update={
                    "last_attempt_at": ensure_utc(at),
                    "attempt_count": event.attempt_count + 1,
                    "last_error": error,
                    "next_attempt_at": (
                        ensure_utc(next_attempt_at) if next_attempt_at is not None else None
                    ),
                    "dead_lettered_at": ensure_utc(at) if dead_letter else None,
                    "claim_token": None,
                    "claimed_until": None,
                }
            )
            self._outbox[event_id] = updated
            return updated

    def release_notification_claim(self, event_id: str, *, claim_token: str) -> None:
        with self._lock:
            event = self._outbox.get(event_id)
            if event is None or event.claim_token != claim_token:
                return
            self._outbox[event_id] = event.model_copy(
                update={"claim_token": None, "claimed_until": None}
            )

    def next_notification_due_at(self, now: datetime) -> datetime | None:
        now = ensure_utc(now)
        candidates: list[datetime] = []
        with self._lock:
            for event in self._outbox.values():
                if not event.pending:
                    continue
                due = now
                if event.next_attempt_at is not None:
                    due = max(due, ensure_utc(event.next_attempt_at))
                if event.claimed_until is not None:
                    due = max(due, ensure_utc(event.claimed_until))
                candidates.append(due)
        return min(candidates) if candidates else None

    # --- evaluations, claims and ordering -----------------------------------------

    def get_committed_result(self, key: EvaluationKey) -> EvaluationRecord | None:
        return self._evaluations.get(key.identity)

    def latest_committed_evaluation(
        self,
        metric: str,
        scope_key: str | None = None,
    ) -> EvaluationRecord | None:
        if scope_key is None:
            scope_key = canonical_scope_key()
        with self._lock:
            matches = [
                record
                for record in self._evaluations.values()
                if record.key.metric == metric and record.key.scope_key == scope_key
            ]
        if not matches:
            return None
        return max(matches, key=lambda r: ensure_utc(r.effective_at or r.key.eval_at))

    def _stream_id(self, key: EvaluationKey) -> tuple[str, str]:
        return (key.metric, key.scope_key)

    def _stream_condition(self, stream: tuple[str, str]) -> threading.Condition:
        cond = self._stream_conditions.get(stream)
        if cond is None:
            cond = threading.Condition(self._lock)
            self._stream_conditions[stream] = cond
        return cond

    def _drop_claim(self, identity: str, stream: tuple[str, str]) -> None:
        self._claims.pop(identity, None)
        self._claim_expiry.pop(identity, None)
        inflight = self._stream_inflight.get(stream)
        if inflight is not None:
            inflight.pop(identity, None)
            if not inflight:
                self._stream_inflight.pop(stream, None)

    def claim_evaluation(self, key: EvaluationKey) -> EvaluationClaim:
        with self._lock:
            existing = self._evaluations.get(key.identity)
            if existing is not None:
                return EvaluationClaim(
                    EvaluationClaimStatus.ALREADY_COMMITTED,
                    key=key,
                    record=existing,
                )
            stream = self._stream_id(key)
            if key.identity in self._claims:
                expiry = self._claim_expiry.get(key.identity)
                if expiry is None or expiry > datetime.now(UTC):
                    return EvaluationClaim(EvaluationClaimStatus.IN_PROGRESS, key=key)
                self._drop_claim(key.identity, stream)
            token = uuid.uuid4().hex
            self._claims[key.identity] = token
            if self.claim_ttl is not None:
                self._claim_expiry[key.identity] = datetime.now(UTC) + timedelta(
                    seconds=self.claim_ttl
                )
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
            stream = self._stream_id(key)
            self._drop_claim(key.identity, stream)
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
    def transaction(
        self,
        *,
        evaluation_key: EvaluationKey | None = None,
        claim_token: str | None = None,
    ) -> Iterator[_InMemoryTransaction]:
        tx = _InMemoryTransaction(self, evaluation_key=evaluation_key, claim_token=claim_token)
        try:
            yield tx
            if not tx.closed:
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


# Preferred public name: the store holds more than state.
InMemoryRuntimeStore = InMemoryStateStore
