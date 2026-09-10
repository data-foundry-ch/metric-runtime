"""Persistence protocols.

Warehouse/lake: historical business facts.
Runtime stores: what metric-runtime currently believes / has already acted upon.

Observation, state, and incidents have different retention and concurrency
characteristics — keep the protocols separable even when one in-memory
object implements all three.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from metric_runtime.models import Incident, KPIState, KPIStatus


def canonical_scope_key(scope: dict[str, str] | None = None) -> str:
    """Stable key for metric+scope identity (idempotency / incident dedup)."""
    if not scope:
        return ""
    return "|".join(f"{key}={scope[key]}" for key in sorted(scope))


@runtime_checkable
class ObservationStore(Protocol):
    def get_history(self, metric: str, scope_key: str = "") -> list[KPIStatus]: ...

    def append_observation(self, metric: str, status: KPIStatus, scope_key: str = "") -> None: ...

    def has_observation(self, metric: str, as_of: str, scope_key: str = "") -> bool: ...


@runtime_checkable
class MetricStateStore(Protocol):
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
class StateStore(ObservationStore, MetricStateStore, IncidentStore, Protocol):
    """Composed runtime store (back-compat name for the full surface)."""

    ...
