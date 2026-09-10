"""In-memory runtime store — zero infrastructure."""

from __future__ import annotations

from metric_runtime.models import Incident, IncidentState, KPIState, KPIStatus
from metric_runtime.stores.base import canonical_scope_key

_ACTIVE_INCIDENT_STATES = {
    IncidentState.OPEN,
    IncidentState.DETECTED,
    IncidentState.ACKNOWLEDGED,
}


class InMemoryStateStore:
    """Process-local observation + state + incident store for demos and tests."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], KPIState] = {}
        self._history: dict[tuple[str, str], list[KPIStatus]] = {}
        self._incidents: dict[str, Incident] = {}
        self._resolved_at: dict[tuple[str, str], object] = {}
        self._incident_seq = 0

    def get_state(self, metric: str, scope_key: str = "") -> KPIState:
        return self._states.get((metric, scope_key), KPIState.NORMAL)

    def set_state(self, metric: str, state: KPIState, scope_key: str = "") -> None:
        self._states[(metric, scope_key)] = state

    def get_history(self, metric: str, scope_key: str = "") -> list[KPIStatus]:
        return list(self._history.get((metric, scope_key), []))

    def append_observation(self, metric: str, status: KPIStatus, scope_key: str = "") -> None:
        self._history.setdefault((metric, scope_key), []).append(status)

    def has_observation(self, metric: str, as_of: str, scope_key: str = "") -> bool:
        return any(item.as_of == as_of for item in self._history.get((metric, scope_key), []))

    def get_resolved_at(self, metric: str, scope_key: str = ""):
        return self._resolved_at.get((metric, scope_key))

    def set_resolved_at(self, metric: str, scope_key: str, at) -> None:
        self._resolved_at[(metric, scope_key)] = at

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
        matches.sort(key=lambda i: i.updated_at or i.opened_at or i.first_detected or "")
        return matches[-1]
