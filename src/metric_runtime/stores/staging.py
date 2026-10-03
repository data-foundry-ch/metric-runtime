"""Shared staged-transaction logic for runtime stores.

A staged transaction buffers mutations in memory and merges them with the
store's committed view for reads (read-your-writes). Concrete stores supply
the committed reads, id allocation and the atomic ``_apply``.
"""

from __future__ import annotations

from metric_runtime.exceptions import MetricRuntimeError
from metric_runtime.identity import EvaluationKey, canonical_scope_key, ensure_utc
from metric_runtime.models import (
    EvaluationRecord,
    Incident,
    IncidentState,
    MetricStateRecord,
    OutboxEvent,
    StoredObservation,
)

ACTIVE_INCIDENT_STATES = frozenset(
    {
        IncidentState.OPEN,
        IncidentState.DETECTED,
        IncidentState.ACKNOWLEDGED,
    }
)


def latest_incident(candidates: list[Incident]) -> Incident | None:
    """Most recently touched incident (updated_at, opened_at, first_detected)."""
    if not candidates:
        return None

    def _sort_key(i: Incident) -> str:
        stamp = i.updated_at or i.opened_at or i.first_detected
        return ensure_utc(stamp).isoformat()

    return sorted(candidates, key=_sort_key)[-1]


class StagedTransaction:
    """Base class: stage mutations, merge with committed reads, apply on commit."""

    def __init__(self) -> None:
        self._obs: dict[str, StoredObservation] = {}
        self._states: dict[tuple[str, str], MetricStateRecord] = {}
        self._incidents: dict[str, Incident] = {}
        self._outbox: dict[str, OutboxEvent] = {}
        self._evaluations: dict[str, EvaluationRecord] = {}
        self._committed = False
        self._rolled_back = False

    # --- hooks for concrete stores -------------------------------------------------

    def _committed_observation(self, key: EvaluationKey) -> StoredObservation | None:
        raise NotImplementedError

    def _committed_result(self, key: EvaluationKey) -> EvaluationRecord | None:
        raise NotImplementedError

    def _committed_history(self, metric: str, scope_key: str) -> list[StoredObservation]:
        raise NotImplementedError

    def _committed_state(self, metric: str, scope_key: str) -> MetricStateRecord:
        raise NotImplementedError

    def _committed_active_incidents(self, metric: str, scope_key: str) -> list[Incident]:
        raise NotImplementedError

    def _committed_incident(self, incident_id: str) -> Incident | None:
        raise NotImplementedError

    def _committed_notification_by_key(self, event_key: str) -> OutboxEvent | None:
        raise NotImplementedError

    def _allocate_incident_id(self) -> str:
        raise NotImplementedError

    def _allocate_outbox_id(self) -> str:
        raise NotImplementedError

    def _apply(self) -> None:
        """Atomically persist everything staged. Raise to abort."""
        raise NotImplementedError

    # --- reads (staged first, then committed) --------------------------------------

    def get_observation(self, key: EvaluationKey) -> StoredObservation | None:
        staged = self._obs.get(key.identity)
        if staged is not None:
            return staged
        return self._committed_observation(key)

    def get_committed_result(self, key: EvaluationKey) -> EvaluationRecord | None:
        staged = self._evaluations.get(key.identity)
        if staged is not None:
            return staged
        return self._committed_result(key)

    def get_history(self, metric: str, scope_key: str = "") -> list[StoredObservation]:
        merged: dict[str, StoredObservation] = {
            item.key.identity: item for item in self._committed_history(metric, scope_key)
        }
        for obs_id, obs in self._obs.items():
            if obs.key.metric == metric and obs.key.scope_key == scope_key:
                merged[obs_id] = obs
        return list(merged.values())

    def get_state_record(self, metric: str, scope_key: str = "") -> MetricStateRecord:
        staged = self._states.get((metric, scope_key))
        if staged is not None:
            return staged
        return self._committed_state(metric, scope_key)

    def find_active_incident(self, metric: str, scope_key: str = "") -> Incident | None:
        by_id = {
            i.id: i for i in self._committed_active_incidents(metric, scope_key) if i.id is not None
        }
        for incident in self._incidents.values():
            if incident.id is not None:
                by_id[incident.id] = incident
        matches = [
            i
            for i in by_id.values()
            if i.primary_metric == metric
            and canonical_scope_key(i.scope) == scope_key
            and i.state in ACTIVE_INCIDENT_STATES
        ]
        return latest_incident(matches)

    def get_incident(self, incident_id: str) -> Incident | None:
        if incident_id in self._incidents:
            return self._incidents[incident_id]
        return self._committed_incident(incident_id)

    # --- staging -------------------------------------------------------------------

    def stage_observation(self, observation: StoredObservation) -> None:
        self._ensure_open()
        self._obs[observation.key.identity] = observation

    def stage_state_record(self, record: MetricStateRecord) -> None:
        self._ensure_open()
        self._states[(record.metric, record.scope_key)] = record

    def stage_incident(self, incident: Incident) -> Incident:
        self._ensure_open()
        if not incident.id:
            incident = incident.model_copy(update={"id": self._allocate_incident_id()})
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
            existing = self._committed_notification_by_key(event.event_key)
            if existing is not None:
                return existing
        if not event.id:
            event = event.model_copy(update={"id": self._allocate_outbox_id()})
        assert event.id is not None
        self._outbox[event.id] = event
        return event

    def stage_evaluation_record(self, record: EvaluationRecord) -> None:
        self._ensure_open()
        self._evaluations[record.key.identity] = record

    # --- lifecycle -----------------------------------------------------------------

    def commit(self) -> None:
        self._ensure_open()
        self._apply()
        self._committed = True

    def rollback(self) -> None:
        self._rolled_back = True
        self._obs.clear()
        self._states.clear()
        self._incidents.clear()
        self._outbox.clear()
        self._evaluations.clear()

    @property
    def closed(self) -> bool:
        return self._committed or self._rolled_back

    def _ensure_open(self) -> None:
        if self._committed or self._rolled_back:
            raise MetricRuntimeError("Transaction is already closed")
