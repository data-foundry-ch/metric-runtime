"""Notification extension points.

Core metric-runtime does not depend on Slack/Teams/email SDKs.

Delivery guarantees:
- Runtime commits are idempotent per EvaluationKey.
- Outbox intent is deduplicated per meaningful transition (event_key).
- External notification delivery is at-least-once unless the notifier
  honors ``idempotency_key``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from metric_runtime.models import Incident, OutboxEvent


@runtime_checkable
class Notifier(Protocol):
    def notify(
        self,
        incident: Incident,
        *,
        idempotency_key: str | None = None,
    ) -> None: ...


@runtime_checkable
class EventNotifier(Protocol):
    """Optional richer protocol: receive the whole outbox event.

    Outbox delivery prefers ``notify_event`` when a notifier implements it, so
    adapters also see the event kind, transition and stable ``event_key``.
    Raising marks the attempt failed (retried with backoff).
    """

    def notify_event(self, event: OutboxEvent) -> None: ...


class RecordingNotifier:
    """Collect notifications for tests."""

    def __init__(self) -> None:
        self.incidents: list[Incident] = []
        self.idempotency_keys: list[str | None] = []

    def notify(
        self,
        incident: Incident,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        self.incidents.append(incident)
        self.idempotency_keys.append(idempotency_key)


class NullNotifier:
    """No-op notifier for local runs."""

    def notify(
        self,
        incident: Incident,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        return None


class LoggingNotifier:
    """Emit incidents to the standard library logger."""

    def __init__(self, logger_name: str = "metric_runtime.notifications") -> None:
        import logging

        self._log = logging.getLogger(logger_name)

    def notify(
        self,
        incident: Incident,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        self._log.info(
            "incident %s metric=%s explanatory=%s owner=%s state=%s impact=%.2f key=%s",
            incident.id,
            incident.primary_metric,
            incident.explanatory_kpi,
            incident.owner,
            incident.state.value,
            incident.estimated_impact,
            idempotency_key,
        )
