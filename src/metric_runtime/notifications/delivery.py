"""Leased outbox delivery.

state transaction -> persist outbox event -> commit -> delivery (here)
-> mark delivered / record attempt with backoff / dead-letter.

Delivery never runs inside a state transaction. External delivery is
at-least-once unless the notifier honors the idempotency key.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from metric_runtime.exceptions import NotificationLeaseLostError
from metric_runtime.models import OutboxEvent

__all__ = ["DeliveryReport", "NotificationPolicy", "deliver_pending"]

_log = logging.getLogger("metric_runtime.notifications")


@dataclass(frozen=True)
class NotificationPolicy:
    """Retry policy for outbox delivery.

    ``max_attempts=None`` never dead-letters. A zero ``backoff_initial`` makes
    failed events immediately eligible again (the library default).
    """

    max_attempts: int | None = None
    backoff_initial: timedelta = timedelta(0)
    backoff_max: timedelta = timedelta(hours=1)
    lease: timedelta = timedelta(minutes=5)

    def backoff_for(self, attempt_count: int) -> timedelta:
        """Delay after the ``attempt_count``-th failed attempt (1-based)."""
        if self.backoff_initial <= timedelta(0):
            return timedelta(0)
        exponent = max(0, attempt_count - 1)
        delay = self.backoff_initial * (2**exponent)
        return min(delay, self.backoff_max)


@dataclass
class DeliveryReport:
    delivered: list[OutboxEvent] = field(default_factory=list)
    failed: list[OutboxEvent] = field(default_factory=list)
    dead_lettered: list[OutboxEvent] = field(default_factory=list)
    lease_lost: int = 0


def _send(notifier, event: OutboxEvent) -> None:
    notify_event = getattr(notifier, "notify_event", None)
    if callable(notify_event):
        notify_event(event)
        return
    if event.incident is not None:
        notifier.notify(event.incident, idempotency_key=event.event_key or event.id)


def deliver_pending(
    store,
    notifier,
    *,
    policy: NotificationPolicy | None = None,
    limit: int | None = None,
    clock: Callable[[], datetime] | None = None,
) -> DeliveryReport:
    """Claim due outbox events, send them, then mark delivered or retry."""
    policy = policy or NotificationPolicy()
    clock = clock or (lambda: datetime.now(UTC))
    report = DeliveryReport()
    claimed = store.claim_pending_notifications(now=clock(), limit=limit, lease=policy.lease)
    for event in claimed:
        assert event.id is not None
        token = event.claim_token
        try:
            _send(notifier, event)
        except Exception as exc:  # noqa: BLE001 - record and continue
            now = clock()
            attempts = event.attempt_count + 1
            dead = policy.max_attempts is not None and attempts >= policy.max_attempts
            next_at = None if dead else now + policy.backoff_for(attempts)
            try:
                updated = store.record_notification_attempt(
                    event.id,
                    at=now,
                    error=str(exc)[:500],
                    claim_token=token,
                    next_attempt_at=next_at,
                    dead_letter=dead,
                )
            except NotificationLeaseLostError:
                report.lease_lost += 1
                _log.warning("outbox event %s lease lost while recording failure", event.id)
                continue
            if dead:
                report.dead_lettered.append(updated)
                _log.error(
                    "outbox event %s dead-lettered after %d attempts: %s",
                    event.id,
                    attempts,
                    exc,
                )
            else:
                report.failed.append(updated)
                _log.warning(
                    "outbox event %s delivery failed (attempt %d), retry at %s: %s",
                    event.id,
                    attempts,
                    next_at.isoformat() if next_at else "-",
                    exc,
                )
            continue
        try:
            report.delivered.append(
                store.mark_notification_delivered(event.id, at=clock(), claim_token=token)
            )
        except NotificationLeaseLostError:
            report.lease_lost += 1
            _log.warning("outbox event %s lease lost before marking delivered", event.id)
        except Exception:
            # Sent but not acknowledged: release the lease so the event stays
            # immediately re-deliverable (at-least-once).
            if token is not None:
                try:
                    store.release_notification_claim(event.id, claim_token=token)
                except Exception:  # noqa: BLE001
                    _log.exception("could not release lease for outbox event %s", event.id)
            raise
    return report
