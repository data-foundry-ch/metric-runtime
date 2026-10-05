"""Notifier contract, built-in logging/null notifiers and outbox delivery.

Channel-specific notifiers live in adapters; ``WebhookNotifier`` stays
importable from here.
"""

from __future__ import annotations

from typing import Any

from metric_runtime.notifications.base import (
    EventNotifier,
    LoggingNotifier,
    Notifier,
    NullNotifier,
    RecordingNotifier,
)
from metric_runtime.notifications.delivery import (
    DeliveryReport,
    NotificationPolicy,
    deliver_pending,
)

__all__ = [
    "DeliveryReport",
    "EventNotifier",
    "LoggingNotifier",
    "NotificationPolicy",
    "Notifier",
    "NullNotifier",
    "RecordingNotifier",
    "WebhookDeliveryError",
    "WebhookNotifier",
    "deliver_pending",
]

_LAZY = {"WebhookDeliveryError", "WebhookNotifier"}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from metric_runtime.adapters.webhook import notifier

        return getattr(notifier, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
