"""State store abstractions.

Warehouse/lake: historical business facts.
StateStore: what metric-runtime currently believes / has already acted upon.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from metric_runtime.models import Incident, KPIState, KPIStatus


@runtime_checkable
class StateStore(Protocol):
    def get_state(self, metric: str, scope_key: str = "") -> KPIState: ...

    def set_state(self, metric: str, state: KPIState, scope_key: str = "") -> None: ...

    def get_history(self, metric: str, scope_key: str = "") -> list[KPIStatus]: ...

    def append_observation(self, metric: str, status: KPIStatus, scope_key: str = "") -> None: ...

    def get_incident(self, incident_id: str) -> Incident | None: ...

    def upsert_incident(self, incident: Incident) -> Incident: ...

    def list_open_incidents(self) -> list[Incident]: ...
