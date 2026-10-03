"""Leased outbox delivery: exclusivity, lost leases, reclaim, backoff, dead-letter."""

from __future__ import annotations

from datetime import timedelta

import pytest

from _runtime_helpers import ts
from metric_runtime.exceptions import NotificationLeaseLostError
from metric_runtime.models import Incident, IncidentState, OutboxEvent
from metric_runtime.notifications import NotificationPolicy, RecordingNotifier, deliver_pending

NOW = ts(2026, 5, 15, 12, 0)
LEASE = timedelta(minutes=5)


def _incident() -> Incident:
    return Incident(
        id="inc-x",
        primary_metric="profit_margin",
        explanatory_kpi="basket_cliff",
        owner="finance",
        state=IncidentState.OPEN,
        first_detected=NOW,
    )


def _enqueue(store, key: str, *, minute: int = 0) -> OutboxEvent:
    return store.enqueue_notification(
        OutboxEvent(
            event_key=key,
            kind="incident_opened",
            metric="profit_margin",
            incident_id="inc-x",
            incident=_incident(),
            message=key,
            created_at=NOW + timedelta(minutes=minute),
        )
    )


class FailingNotifier:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def notify(self, incident, *, idempotency_key=None) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(f"down #{self.calls}")


def test_claim_is_exclusive_until_lease_expires(store_factory):
    store = store_factory()
    _enqueue(store, "a")
    _enqueue(store, "b", minute=1)
    first = store.claim_pending_notifications(now=NOW, limit=None, lease=LEASE)
    assert [e.event_key for e in first] == ["a", "b"]
    assert all(e.claim_token and e.claimed_until == NOW + LEASE for e in first)
    assert store.claim_pending_notifications(now=NOW, lease=LEASE) == []
    later = NOW + LEASE
    reclaimed = store.claim_pending_notifications(now=later, lease=LEASE)
    assert [e.event_key for e in reclaimed] == ["a", "b"]
    assert {e.claim_token for e in reclaimed}.isdisjoint({e.claim_token for e in first})


def test_claim_respects_limit_and_order(store_factory):
    store = store_factory()
    for i, key in enumerate(["k1", "k2", "k3"]):
        _enqueue(store, key, minute=i)
    claimed = store.claim_pending_notifications(now=NOW, limit=2, lease=LEASE)
    assert [e.event_key for e in claimed] == ["k1", "k2"]
    rest = store.claim_pending_notifications(now=NOW, limit=2, lease=LEASE)
    assert [e.event_key for e in rest] == ["k3"]


def test_lost_lease_rejects_writes(store_factory):
    store = store_factory()
    _enqueue(store, "a")
    (stale,) = store.claim_pending_notifications(now=NOW, lease=LEASE)
    (fresh,) = store.claim_pending_notifications(now=NOW + LEASE, lease=LEASE)
    assert stale.id == fresh.id
    with pytest.raises(NotificationLeaseLostError):
        store.mark_notification_delivered(stale.id, at=NOW, claim_token=stale.claim_token)
    with pytest.raises(NotificationLeaseLostError):
        store.record_notification_attempt(
            stale.id, at=NOW, error="x", claim_token=stale.claim_token
        )
    delivered = store.mark_notification_delivered(
        fresh.id, at=NOW + LEASE, claim_token=fresh.claim_token
    )
    assert delivered.delivered_at == NOW + LEASE
    assert delivered.claim_token is None
    assert store.list_pending_notifications() == []


def test_release_makes_event_immediately_claimable(store_factory):
    store = store_factory()
    _enqueue(store, "a")
    (claimed,) = store.claim_pending_notifications(now=NOW, lease=LEASE)
    store.release_notification_claim(claimed.id, claim_token=claimed.claim_token)
    assert len(store.claim_pending_notifications(now=NOW, lease=LEASE)) == 1


def test_backoff_then_success(store_factory):
    store = store_factory()
    _enqueue(store, "a")
    policy = NotificationPolicy(
        max_attempts=5, backoff_initial=timedelta(seconds=30), backoff_max=timedelta(minutes=10)
    )
    notifier = FailingNotifier(failures=2)
    clock = {"now": NOW}

    def tick():
        return clock["now"]

    report = deliver_pending(store, notifier, policy=policy, clock=tick)
    assert len(report.failed) == 1
    (pending,) = store.list_pending_notifications()
    assert pending.attempt_count == 1
    assert pending.next_attempt_at == NOW + timedelta(seconds=30)
    assert store.next_notification_due_at(NOW) == NOW + timedelta(seconds=30)

    # Not yet due: nothing is claimed.
    assert deliver_pending(store, notifier, policy=policy, clock=tick).failed == []
    assert notifier.calls == 1

    clock["now"] = NOW + timedelta(seconds=30)
    report = deliver_pending(store, notifier, policy=policy, clock=tick)
    (pending,) = store.list_pending_notifications()
    assert pending.attempt_count == 2
    assert pending.next_attempt_at == clock["now"] + timedelta(seconds=60)

    clock["now"] = pending.next_attempt_at
    report = deliver_pending(store, notifier, policy=policy, clock=tick)
    assert len(report.delivered) == 1
    assert store.list_pending_notifications() == []
    assert store.next_notification_due_at(clock["now"]) is None


def test_dead_letter_after_max_attempts(store_factory):
    store = store_factory()
    _enqueue(store, "a")
    policy = NotificationPolicy(max_attempts=2, backoff_initial=timedelta(0))
    notifier = FailingNotifier(failures=10)
    first = deliver_pending(store, notifier, policy=policy, clock=lambda: NOW)
    assert len(first.failed) == 1
    second = deliver_pending(store, notifier, policy=policy, clock=lambda: NOW)
    assert len(second.dead_lettered) == 1
    assert second.dead_lettered[0].dead_lettered_at == NOW
    assert store.list_pending_notifications() == []
    assert store.next_notification_due_at(NOW) is None
    assert deliver_pending(store, notifier, policy=policy, clock=lambda: NOW).failed == []
    assert notifier.calls == 2


def test_next_due_reports_now_for_immediately_due_events(store_factory):
    store = store_factory()
    assert store.next_notification_due_at(NOW) is None
    _enqueue(store, "a")
    assert store.next_notification_due_at(NOW) == NOW
    store.claim_pending_notifications(now=NOW, lease=LEASE)
    assert store.next_notification_due_at(NOW) == NOW + LEASE


def test_default_policy_redelivers_on_next_pass(store_factory):
    store = store_factory()
    _enqueue(store, "a")
    notifier = FailingNotifier(failures=1)
    assert deliver_pending(store, notifier).delivered == []
    assert len(deliver_pending(store, notifier).delivered) == 1


def test_recording_notifier_receives_event_key_as_idempotency_key(store_factory):
    store = store_factory()
    _enqueue(store, "stable-key")
    notifier = RecordingNotifier()
    deliver_pending(store, notifier)
    assert notifier.idempotency_keys == ["stable-key"]
