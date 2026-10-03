"""Persistence protocols.

Warehouse/lake: historical business facts.
Runtime stores: what metric-runtime currently believes / has already acted upon.

Observation, state, incidents, evaluations and notification outbox have
different retention and concurrency characteristics. Transactional commit makes
one EvaluationKey's mutations atomic from the runtime's perspective.

Per-(metric, scope) streams additionally enforce monotonic ``effective_at``
ordering so a newer window cannot be overwritten by an older one.

A ``RuntimeStore`` is never the analytical source: it only holds runtime
conclusions (observations, state, evaluations, incidents, outbox).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

from metric_runtime.identity import EvaluationKey, canonical_scope_key
from metric_runtime.models import (
    EvaluationRecord,
    Incident,
    MetricStateRecord,
    OutboxEvent,
    StoredObservation,
)

__all__ = [
    "EvaluationClaim",
    "EvaluationClaimStatus",
    "IncidentStore",
    "MetricStateStore",
    "NotificationOutbox",
    "ObservationStore",
    "RuntimeStore",
    "RuntimeTransaction",
    "StateStore",
    "TransactionalRuntimeStore",
    "canonical_scope_key",
]


class EvaluationClaimStatus(str, Enum):
    ACQUIRED = "acquired"
    ALREADY_COMMITTED = "already_committed"
    IN_PROGRESS = "in_progress"


class EvaluationClaim:
    """Result of trying to own an EvaluationKey for transition processing."""

    def __init__(
        self,
        status: EvaluationClaimStatus,
        *,
        key: EvaluationKey,
        record: EvaluationRecord | None = None,
        token: str | None = None,
    ) -> None:
        self.status = status
        self.key = key
        self.record = record
        self.token = token

    @property
    def acquired(self) -> bool:
        return self.status == EvaluationClaimStatus.ACQUIRED


@runtime_checkable
class ObservationStore(Protocol):
    def get_observation(self, key: EvaluationKey) -> StoredObservation | None: ...

    def put_observation(self, observation: StoredObservation) -> StoredObservation: ...

    def get_history(self, metric: str, scope_key: str = "") -> list[StoredObservation]: ...


@runtime_checkable
class MetricStateStore(Protocol):
    def get_state_record(self, metric: str, scope_key: str = "") -> MetricStateRecord: ...

    def set_state_record(self, record: MetricStateRecord) -> None: ...


@runtime_checkable
class IncidentStore(Protocol):
    def get_incident(self, incident_id: str) -> Incident | None: ...

    def upsert_incident(self, incident: Incident) -> Incident: ...

    def list_open_incidents(self) -> list[Incident]: ...

    def find_active_incident(
        self,
        metric: str,
        scope_key: str = "",
    ) -> Incident | None: ...


@runtime_checkable
class NotificationOutbox(Protocol):
    """Durable notification intent with leased, retryable delivery.

    ``claim_pending_notifications`` persists a lease (``claim_token`` /
    ``claimed_until``) on each returned event. Writes that pass a
    ``claim_token`` only apply while that lease is still held; otherwise they
    raise :class:`~metric_runtime.exceptions.NotificationLeaseLostError`.
    """

    def enqueue_notification(self, event: OutboxEvent) -> OutboxEvent: ...

    def list_pending_notifications(self) -> list[OutboxEvent]: ...

    def claim_pending_notifications(
        self,
        *,
        now: datetime,
        limit: int | None = None,
        lease: timedelta,
    ) -> list[OutboxEvent]: ...

    def mark_notification_delivered(
        self,
        event_id: str,
        *,
        at: datetime,
        claim_token: str | None = None,
    ) -> OutboxEvent: ...

    def record_notification_attempt(
        self,
        event_id: str,
        *,
        at: datetime,
        error: str | None = None,
        claim_token: str | None = None,
        next_attempt_at: datetime | None = None,
        dead_letter: bool = False,
    ) -> OutboxEvent: ...

    def release_notification_claim(self, event_id: str, *, claim_token: str) -> None: ...

    def next_notification_due_at(self, now: datetime) -> datetime | None: ...


@runtime_checkable
class RuntimeTransaction(Protocol):
    """Staged mutations for one atomic runtime commit."""

    def get_observation(self, key: EvaluationKey) -> StoredObservation | None: ...

    def get_committed_result(self, key: EvaluationKey) -> EvaluationRecord | None: ...

    def get_history(self, metric: str, scope_key: str = "") -> list[StoredObservation]: ...

    def get_state_record(self, metric: str, scope_key: str = "") -> MetricStateRecord: ...

    def find_active_incident(self, metric: str, scope_key: str = "") -> Incident | None: ...

    def get_incident(self, incident_id: str) -> Incident | None: ...

    def stage_observation(self, observation: StoredObservation) -> None: ...

    def stage_state_record(self, record: MetricStateRecord) -> None: ...

    def stage_incident(self, incident: Incident) -> Incident: ...

    def stage_notification(self, event: OutboxEvent) -> OutboxEvent: ...

    def stage_evaluation_record(self, record: EvaluationRecord) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@runtime_checkable
class TransactionalRuntimeStore(Protocol):
    def transaction(
        self,
        *,
        evaluation_key: EvaluationKey | None = None,
        claim_token: str | None = None,
    ) -> AbstractContextManager[RuntimeTransaction]:
        """Open a staged transaction.

        When ``evaluation_key`` / ``claim_token`` are passed, ``commit()``
        verifies the claim is still held and the stream is still committable.
        """
        ...

    def claim_evaluation(self, key: EvaluationKey) -> EvaluationClaim: ...

    def release_evaluation_claim(self, key: EvaluationKey, *, token: str | None = None) -> None: ...

    def get_committed_result(self, key: EvaluationKey) -> EvaluationRecord | None: ...

    def latest_committed_evaluation(
        self,
        metric: str,
        scope_key: str | None = None,
    ) -> EvaluationRecord | None:
        """Committed evaluation with the highest ``effective_at`` (scheduler cursor).

        ``scope_key=None`` means the unscoped stream (``canonical_scope_key({})``).
        """
        ...

    def ordered_stream_commit(
        self,
        key: EvaluationKey,
        *,
        timeout: float | None = 30.0,
    ) -> AbstractContextManager[None]:
        """Wait until this evaluation is next for its (metric, scope) stream."""
        ...


@runtime_checkable
class RuntimeStore(
    ObservationStore,
    MetricStateStore,
    IncidentStore,
    NotificationOutbox,
    TransactionalRuntimeStore,
    Protocol,
):
    """Durable home for Metric Runtime conclusions.

    Stores observations, metric state, committed evaluations, incidents and
    the notification outbox. Never the analytical source.
    """

    def close(self) -> None: ...


# Back-compat name: the store holds more than state, prefer ``RuntimeStore``.
StateStore = RuntimeStore
