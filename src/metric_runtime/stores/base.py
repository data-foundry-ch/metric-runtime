"""Persistence protocols.

Warehouse/lake: historical business facts.
Runtime stores: what metric-runtime currently believes / has already acted upon.

Observation, state, incidents, and notification outbox have different retention
and concurrency characteristics — keep the protocols separable even when one
in-memory object implements all of them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from metric_runtime.identity import EvaluationKey, canonical_scope_key
from metric_runtime.models import (
    Incident,
    KPIState,
    KPIStatus,
    MetricStateRecord,
    OutboxEvent,
    StoredObservation,
)

__all__ = [
    "EvaluationKey",
    "IncidentStore",
    "MetricStateStore",
    "NotificationOutbox",
    "ObservationStore",
    "StateStore",
    "canonical_scope_key",
]


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


@runtime_checkable
class StateStore(ObservationStore, MetricStateStore, IncidentStore, NotificationOutbox, Protocol):
    """Composed runtime store (back-compat name for the full surface)."""

    ...
