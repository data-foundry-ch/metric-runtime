"""Notification extension points.

Core metric-runtime does not depend on Slack/Teams/email SDKs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from metric_runtime.models import Incident


@runtime_checkable
class Notifier(Protocol):
    def notify(self, incident: Incident) -> None: ...


class RecordingNotifier:
    """Collect notifications for tests."""

    def __init__(self) -> None:
        self.incidents: list[Incident] = []

    def notify(self, incident: Incident) -> None:
        self.incidents.append(incident)


class NullNotifier:
    """No-op notifier for local runs."""

    def notify(self, incident: Incident) -> None:
        return None


class LoggingNotifier:
    """Emit incidents to the standard library logger."""

    def __init__(self, logger_name: str = "metric_runtime.notifications") -> None:
        import logging

        self._log = logging.getLogger(logger_name)

    def notify(self, incident: Incident) -> None:
        self._log.info(
            "incident %s metric=%s explanatory=%s owner=%s state=%s impact=%.2f",
            incident.id,
            incident.primary_metric,
            incident.explanatory_kpi,
            incident.owner,
            incident.state.value,
            incident.estimated_impact,
        )
