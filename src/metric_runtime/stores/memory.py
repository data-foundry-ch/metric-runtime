"""In-memory runtime store — zero infrastructure."""

from __future__ import annotations

from datetime import UTC, datetime

from metric_runtime.identity import EvaluationKey, canonical_scope_key, ensure_utc
from metric_runtime.models import (
    Incident,
    IncidentState,
    KPIState,
    KPIStatus,
    MetricStateRecord,
    OutboxEvent,
    StoredObservation,
)

_ACTIVE_INCIDENT_STATES = {
    IncidentState.OPEN,
    IncidentState.DETECTED,
    IncidentState.ACKNOWLEDGED,
}


class InMemoryStateStore:
    """Process-local observation + state + incident + outbox store."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], MetricStateRecord] = {}
        self._observations: dict[str, StoredObservation] = {}
        self._history: dict[tuple[str, str], list[str]] = {}
        self._incidents: dict[str, Incident] = {}
        self._outbox: dict[str, OutboxEvent] = {}
        self._incident_seq = 0
        self._outbox_seq = 0

    @staticmethod
    def _obs_id(key: EvaluationKey) -> str:
        return f"{key.metric}|{key.scope_key}|{key.window_id}"

    def get_observation(self, key: EvaluationKey) -> StoredObservation | None:
        return self._observations.get(self._obs_id(key))

    def put_observation(self, observation: StoredObservation) -> StoredObservation:
        obs_id = self._obs_id(observation.key)
        self._observations[obs_id] = observation
        hist_key = (observation.key.metric, observation.key.scope_key)
        ids = self._history.setdefault(hist_key, [])
        if obs_id not in ids:
            ids.append(obs_id)
        return observation

    def get_history(self, metric: str, scope_key: str = "") -> list[StoredObservation]:
        return [
            self._observations[obs_id]
            for obs_id in self._history.get((metric, scope_key), [])
            if obs_id in self._observations
        ]

    def append_observation(self, metric: str, status: KPIStatus, scope_key: str = "") -> None:
        """Back-compat helper — prefer put_observation with an EvaluationKey."""
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
        )

    def set_state_record(self, record: MetricStateRecord) -> None:
        self._states[(record.metric, record.scope_key)] = record

    def get_state(self, metric: str, scope_key: str = "") -> KPIState:
        return self.get_state_record(metric, scope_key).state

    def set_state(self, metric: str, state: KPIState, scope_key: str = "") -> None:
        now = datetime.now(UTC)
        previous = self._states.get((metric, scope_key))
        resolved_at = previous.resolved_at if previous is not None else None
        state_since = previous.state_since if previous is not None else now
        if previous is None or previous.state != state:
            state_since = now
        if state == KPIState.RESOLVED:
            resolved_at = now
        self.set_state_record(
            MetricStateRecord(
                metric=metric,
                scope_key=scope_key,
                scope=previous.scope if previous is not None else {},
                state=state,
                state_since=state_since,
                updated_at=now,
                resolved_at=resolved_at,
            )
        )

    def get_incident(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    def upsert_incident(self, incident: Incident) -> Incident:
        if not incident.id:
            self._incident_seq += 1
            incident = incident.model_copy(update={"id": f"inc-{self._incident_seq:04d}"})
        incident_id = incident.id
        assert incident_id is not None
        self._incidents[incident_id] = incident
        return incident

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
        if not event.id:
            self._outbox_seq += 1
            event = event.model_copy(update={"id": f"out-{self._outbox_seq:04d}"})
        assert event.id is not None
        self._outbox[event.id] = event
        return event

    def list_pending_notifications(self) -> list[OutboxEvent]:
        pending = [e for e in self._outbox.values() if e.delivered_at is None]
        pending.sort(key=lambda e: ensure_utc(e.created_at).isoformat())
        return pending

    def mark_notification_delivered(self, event_id: str, *, at: datetime) -> OutboxEvent:
        event = self._outbox[event_id]
        updated = event.model_copy(update={"delivered_at": ensure_utc(at)})
        self._outbox[event_id] = updated
        return updated
