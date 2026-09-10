"""In-memory state store — zero infrastructure."""

from __future__ import annotations

from metric_runtime.models import Incident, KPIState, KPIStatus


class InMemoryStateStore:
    """Process-local state for demos, tests, and embedding."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], KPIState] = {}
        self._history: dict[tuple[str, str], list[KPIStatus]] = {}
        self._incidents: dict[str, Incident] = {}
        self._incident_seq = 0

    def get_state(self, metric: str, scope_key: str = "") -> KPIState:
        return self._states.get((metric, scope_key), KPIState.NORMAL)

    def set_state(self, metric: str, state: KPIState, scope_key: str = "") -> None:
        self._states[(metric, scope_key)] = state

    def get_history(self, metric: str, scope_key: str = "") -> list[KPIStatus]:
        return list(self._history.get((metric, scope_key), []))

    def append_observation(self, metric: str, status: KPIStatus, scope_key: str = "") -> None:
        self._history.setdefault((metric, scope_key), []).append(status)

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
        from metric_runtime.models import IncidentState

        return [
            i
            for i in self._incidents.values()
            if i.state in {IncidentState.OPEN, IncidentState.DETECTED, IncidentState.ACKNOWLEDGED}
        ]
