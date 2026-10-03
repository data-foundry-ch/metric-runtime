"""WebhookNotifier against a local http.server; outbox retry integration."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from _runtime_helpers import ts
from metric_runtime.config.factory import build_notifier, validate_profile_wiring
from metric_runtime.config.loader import load_connections_config, redact_secrets
from metric_runtime.exceptions import ConfigurationError
from metric_runtime.models import Incident, IncidentState, KPIState, OutboxEvent
from metric_runtime.notifications import (
    EventNotifier,
    LoggingNotifier,
    NotificationPolicy,
    NullNotifier,
    WebhookDeliveryError,
    WebhookNotifier,
    deliver_pending,
)
from metric_runtime.stores import InMemoryRuntimeStore

NOW = ts(2026, 5, 15, 12, 0)


class _Server:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: list[int] = []
        self.delay = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                if outer.delay:
                    threading.Event().wait(outer.delay)
                code = outer.responses.pop(0) if outer.responses else 200
                self.send_response(code)
                if code in (301, 302, 307):
                    self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args) -> None:
                return None

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/hook?token=abc"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[_Server]:
    with _Server() as srv:
        yield srv


def _event(key: str = "evt-key-1") -> OutboxEvent:
    return OutboxEvent(
        id="out-0001",
        event_key=key,
        kind="incident_opened",
        metric="profit_margin",
        incident_id="inc-0001",
        incident=Incident(
            id="inc-0001",
            primary_metric="profit_margin",
            explanatory_kpi="basket_cliff",
            owner="finance",
            state=IncidentState.OPEN,
            first_detected=NOW,
        ),
        previous_state=KPIState.DETECTED,
        current_state=KPIState.OPEN,
        message="Opened incident for profit_margin",
        created_at=NOW,
    )


def test_posts_json_with_idempotency_and_signature(server):
    notifier = WebhookNotifier(server.url, secret="shh", headers={"Authorization": "Bearer t"})
    assert isinstance(notifier, EventNotifier)
    notifier.notify_event(_event())
    (req,) = server.requests
    headers = {k.lower(): v for k, v in req["headers"].items()}
    assert headers["idempotency-key"] == "evt-key-1"
    assert headers["content-type"] == "application/json"
    assert headers["x-metric-runtime-event"] == "incident_opened"
    assert headers["authorization"] == "Bearer t"
    expected = "sha256=" + hmac.new(b"shh", req["body"], hashlib.sha256).hexdigest()
    assert headers["x-metric-runtime-signature"] == expected
    payload = json.loads(req["body"])
    assert payload["event_key"] == "evt-key-1"
    assert payload["kind"] == "incident_opened"
    assert payload["previous_state"] == "DETECTED" and payload["current_state"] == "OPEN"
    assert payload["incident"]["id"] == "inc-0001"
    assert payload["attempt"] == 1


def test_unsigned_by_default(server):
    WebhookNotifier(server.url).notify_event(_event())
    headers = {k.lower() for k in server.requests[0]["headers"]}
    assert "x-metric-runtime-signature" not in headers


@pytest.mark.parametrize("code", [400, 500, 503, 302])
def test_non_2xx_raises_without_leaking_url(server, code):
    server.responses.append(code)
    with pytest.raises(WebhookDeliveryError) as exc:
        WebhookNotifier(server.url).notify_event(_event())
    assert str(code) in str(exc.value)
    assert "token=abc" not in str(exc.value)
    assert len(server.requests) == 1  # redirects are not followed


def test_timeout_raises(server):
    server.delay = 1.0
    with pytest.raises(WebhookDeliveryError, match="timed out|failed"):
        WebhookNotifier(server.url, timeout=0.2).notify_event(_event())


def test_connection_refused_raises():
    with pytest.raises(WebhookDeliveryError, match="failed"):
        WebhookNotifier("http://127.0.0.1:1/hook", timeout=1).notify_event(_event())


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "not a url", "http://"])
def test_rejects_non_http_urls(url):
    with pytest.raises(ConfigurationError):
        WebhookNotifier(url)


def test_repr_hides_url():
    text = repr(WebhookNotifier("https://hooks.example.com/T000/secret-token"))
    assert "secret-token" not in text and "hooks.example.com" in text


def test_outbox_retries_failed_webhook_then_delivers_same_key(server):
    store = InMemoryRuntimeStore()
    store.enqueue_notification(_event().model_copy(update={"id": None}))
    server.responses.extend([503, 200])
    policy = NotificationPolicy(max_attempts=3, backoff_initial=timedelta(seconds=30))
    notifier = WebhookNotifier(server.url)

    first = deliver_pending(store, notifier, policy=policy, clock=lambda: NOW)
    assert len(first.failed) == 1 and "503" in first.failed[0].last_error
    second = deliver_pending(
        store, notifier, policy=policy, clock=lambda: NOW + timedelta(seconds=30)
    )
    assert len(second.delivered) == 1
    keys = [
        {k.lower(): v for k, v in r["headers"].items()}["idempotency-key"] for r in server.requests
    ]
    assert keys == ["evt-key-1", "evt-key-1"]
    assert [json.loads(r["body"])["attempt"] for r in server.requests] == [1, 2]


def _connections(tmp_path: Path, text: str):
    path = tmp_path / "connections.yaml"
    path.write_text(text, encoding="utf-8")
    cfg, _ = load_connections_config(
        path, env={"HOOK_URL": "https://hooks.example.com/x", "HOOK_SECRET": "s"}
    )
    return cfg


def test_notifier_profile_role(tmp_path):
    cfg = _connections(
        tmp_path,
        "connections:\n"
        "  alerts:\n    type: webhook\n    url: ${HOOK_URL}\n    secret: ${HOOK_SECRET}\n"
        "    headers:\n      Authorization: Bearer xyz\n    timeout: 5\n"
        "  mem:\n    type: memory\n"
        "profiles:\n"
        "  hook:\n    notifier: alerts\n"
        "  default: {}\n"
        "  quiet:\n    notifier:\n      type: none\n"
        "  yaml_null:\n    notifier:\n      type: null\n"
        "  wrong:\n    notifier: mem\n",
    )
    hook = build_notifier(cfg.profiles["hook"], cfg)
    assert isinstance(hook, WebhookNotifier) and hook.timeout == 5
    assert isinstance(build_notifier(cfg.profiles["default"], cfg), LoggingNotifier)
    assert isinstance(build_notifier(cfg.profiles["quiet"], cfg), NullNotifier)
    assert isinstance(build_notifier(cfg.profiles["yaml_null"], cfg), NullNotifier)
    errors = validate_profile_wiring(cfg.profiles["wrong"], cfg)
    assert errors and "notifier" in errors[0]


def test_webhook_secrets_redacted_in_config_show():
    shown = redact_secrets(
        {
            "alerts": {
                "type": "webhook",
                "url": "https://x/t0k3n",
                "secret": "s",
                "headers": {"A": "b"},
            }
        }
    )
    blob = str(shown)
    assert "t0k3n" not in blob and "'s'" not in blob and "'b'" not in blob


def test_invalid_webhook_url_in_config(tmp_path):
    cfg = _connections(
        tmp_path,
        "connections:\n  alerts:\n    type: webhook\n    url: file:///tmp/x\n"
        "profiles:\n  p:\n    notifier: alerts\n",
    )
    with pytest.raises(ConfigurationError, match="http"):
        cfg.get_connection("alerts")
