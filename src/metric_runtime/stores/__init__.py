"""State persistence adapters."""

from metric_runtime.identity import EvaluationKey, canonical_scope_json, canonical_scope_key
from metric_runtime.stores.base import (
    IncidentStore,
    MetricStateStore,
    NotificationOutbox,
    ObservationStore,
    StateStore,
)
from metric_runtime.stores.memory import InMemoryStateStore

__all__ = [
    "EvaluationKey",
    "IncidentStore",
    "InMemoryStateStore",
    "MetricStateStore",
    "NotificationOutbox",
    "ObservationStore",
    "StateStore",
    "canonical_scope_json",
    "canonical_scope_key",
]
