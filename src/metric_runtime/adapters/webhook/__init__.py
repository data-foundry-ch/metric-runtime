"""Webhook adapter: signed JSON POST per outbox event."""

from metric_runtime.adapters.webhook.adapter import WebhookAdapter
from metric_runtime.adapters.webhook.config import WebhookConnectionConfig
from metric_runtime.adapters.webhook.notifier import (
    WebhookDeliveryError,
    WebhookNotifier,
    sign_body,
)

__all__ = [
    "WebhookAdapter",
    "WebhookConnectionConfig",
    "WebhookDeliveryError",
    "WebhookNotifier",
    "sign_body",
]
