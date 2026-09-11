"""Persistence protocols.

Warehouse/lake: historical business facts.
Runtime stores: what metric-runtime currently believes / has already acted upon.

Observation, state, incidents, and notification outbox have different retention
and concurrency characteristics. Transactional commit makes one EvaluationKey's
mutations atomic from the runtime's perspective.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from metric_runtime.identity import EvaluationKey, canonical_scope_key
from metric_runtime.models import (
    EvaluationRecord,
    Incident,
    KPIState,
    KPIStatus,
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

    def append_observation(self, metric: str, status: KPIStatus, scope_key: str = "") -> None: ...

    def has_observation(self, metric: str, as_of: str, scope_key: str = "") -> bool: ...


@runtime_checkable
class MetricStateStore(Protocol):
    def get_state_record(self, metric: str, scope_key: str = "") -> MetricStateRecord: ...

    def set_state_record(self, record: MetricStateRecord) -> None: ...

    def get_state(self, metric: str, scope_key: str = "") -> KPIState: ...

    def set_state(self, metric: str, state: KPIState, scope_key: str = "") -> None: ...


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
    def enqueue_notification(self, event: OutboxEvent) -> OutboxEvent: ...

    def list_pending_notifications(self) -> list[OutboxEvent]: ...

    def mark_notification_delivered(self, event_id: str, *, at: datetime) -> OutboxEvent: ...

    def record_notification_attempt(
        self,
        event_id: str,
        *,
        at: datetime,
        error: str | None = None,
    ) -> OutboxEvent: ...


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
    def transaction(self) -> AbstractContextManager[RuntimeTransaction]: ...

    def claim_evaluation(self, key: EvaluationKey) -> EvaluationClaim: ...

    def release_evaluation_claim(self, key: EvaluationKey, *, token: str | None = None) -> None: ...

    def get_committed_result(self, key: EvaluationKey) -> EvaluationRecord | None: ...


@runtime_checkable
class StateStore(
    ObservationStore,
    MetricStateStore,
    IncidentStore,
    NotificationOutbox,
    TransactionalRuntimeStore,
    Protocol,
):
    """Composed runtime store (back-compat name for the full surface)."""

    ...
