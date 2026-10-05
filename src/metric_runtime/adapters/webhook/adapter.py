"""Generic JSON webhook (``notifier`` only, stdlib HTTP client)."""

from __future__ import annotations

from typing import Any

from metric_runtime.adapters.base import AdapterCapabilities, AdapterContext, BaseAdapter
from metric_runtime.adapters.webhook.config import WebhookConnectionConfig

__all__ = ["WebhookAdapter"]


class WebhookAdapter(BaseAdapter):
    type_name = "webhook"
    capabilities = AdapterCapabilities(notifier=True)
    config_model = WebhookConnectionConfig

    def build_notifier(self, config: WebhookConnectionConfig, context: AdapterContext) -> Any:
        from metric_runtime.adapters.webhook.notifier import WebhookNotifier

        return WebhookNotifier(
            config.url.get_secret_value(),
            secret=config.secret.get_secret_value() if config.secret else None,
            headers={k: v.get_secret_value() for k, v in config.headers.items()},
            timeout=config.timeout,
        )
