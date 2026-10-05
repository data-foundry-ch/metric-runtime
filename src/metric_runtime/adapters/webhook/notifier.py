"""Generic JSON webhook notifier (stdlib only).

Each outbox event is POSTed as JSON with:

- ``Idempotency-Key: <event_key>`` — stable across retries, so receivers can
  deduplicate at-least-once delivery;
- ``X-Metric-Runtime-Event: <kind>``;
- optionally ``X-Metric-Runtime-Signature: sha256=<hex>`` — HMAC-SHA256 of the
  exact request body with the shared secret.

Any non-2xx response (redirects are not followed), timeout or connection
error raises :class:`WebhookDeliveryError`, which the outbox records as a
failed attempt and retries with backoff.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from metric_runtime.exceptions import ConfigurationError, MetricRuntimeError
from metric_runtime.models import Incident, OutboxEvent

__all__ = ["WebhookDeliveryError", "WebhookNotifier", "sign_body"]

SIGNATURE_HEADER = "X-Metric-Runtime-Signature"


class WebhookDeliveryError(MetricRuntimeError):
    """The webhook did not acknowledge the event with a 2xx response."""


def sign_body(secret: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` signature for ``body``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class WebhookNotifier:
    """POST outbox events to an HTTP(S) endpoint."""

    def __init__(
        self,
        url: str,
        *,
        secret: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ConfigurationError("Webhook url must be an absolute http(s) URL")
        if timeout <= 0:
            raise ConfigurationError("Webhook timeout must be positive")
        self._url = url
        self._secret = secret or None
        self._headers = dict(headers or {})
        self.timeout = float(timeout)
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self) -> str:
        host = urlsplit(self._url).hostname or "?"
        return f"WebhookNotifier(host={host!r}, signed={self._secret is not None})"

    # --- payloads ----------------------------------------------------------------------

    @staticmethod
    def event_payload(event: OutboxEvent) -> dict[str, Any]:
        return {
            "event_id": event.id,
            "event_key": event.event_key,
            "kind": event.kind,
            "metric": event.metric,
            "incident_id": event.incident_id,
            "previous_state": event.previous_state.value if event.previous_state else None,
            "current_state": event.current_state.value if event.current_state else None,
            "message": event.message,
            "created_at": event.created_at.isoformat(),
            "attempt": event.attempt_count + 1,
            "incident": event.incident.model_dump(mode="json") if event.incident else None,
        }

    # --- Notifier protocols ------------------------------------------------------------

    def notify_event(self, event: OutboxEvent) -> None:
        self._post(
            self.event_payload(event),
            idempotency_key=event.event_key or event.id,
            kind=event.kind,
        )

    def notify(self, incident: Incident, *, idempotency_key: str | None = None) -> None:
        payload = {
            "event_id": None,
            "event_key": idempotency_key,
            "kind": "incident",
            "metric": incident.primary_metric,
            "incident_id": incident.id,
            "created_at": datetime.now(UTC).isoformat(),
            "incident": incident.model_dump(mode="json"),
        }
        self._post(payload, idempotency_key=idempotency_key, kind="incident")

    # --- transport -----------------------------------------------------------------------

    def _post(self, payload: dict[str, Any], *, idempotency_key: str | None, kind: str) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = {
            **self._headers,
            "Content-Type": "application/json",
            "User-Agent": "metric-runtime-webhook",
            "X-Metric-Runtime-Event": kind,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if self._secret is not None:
            headers[SIGNATURE_HEADER] = sign_body(self._secret, body)
        request = urllib.request.Request(self._url, data=body, headers=headers, method="POST")
        # Error messages never include the URL: webhook URLs often embed tokens.
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            raise WebhookDeliveryError(f"webhook returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise WebhookDeliveryError(f"webhook request failed: {exc.reason}") from None
        except TimeoutError:
            raise WebhookDeliveryError(f"webhook timed out after {self.timeout:g}s") from None
        if not 200 <= status < 300:
            raise WebhookDeliveryError(f"webhook returned HTTP {status}")
