"""RuntimeStore contract: identical guarantees for memory and Postgres stores."""

from __future__ import annotations

import threading
import time

import pytest

from _runtime_helpers import engine, patch_anomaly, status, ts
from metric_runtime import KPIState
from metric_runtime.exceptions import (
    EvaluationInProgressError,
    StaleEvaluationError,
    StreamCommitConflict,
)
from metric_runtime.identity import EvaluationKey, canonical_scope_key
from metric_runtime.models import StoredObservation
from metric_runtime.stores.base import EvaluationClaimStatus, RuntimeStore

SCOPE = {"city": "Amsterdam"}
T12 = ts(2026, 5, 15, 12, 0)
T1230 = ts(2026, 5, 15, 12, 30)
T13 = ts(2026, 5, 15, 13, 0)


def test_store_satisfies_runtime_store_protocol(store_factory):
    assert isinstance(store_factory(), RuntimeStore)


def test_persists_observation_state_evaluation_and_incident(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    patch_anomaly(monkeypatch, eng)

    first = eng.process(metric="profit_margin", at=T12, scope=SCOPE)
    second = eng.process(metric="profit_margin", at=T1230, scope=SCOPE)

    assert first.transition == (KPIState.NORMAL, KPIState.DETECTED)
    assert second.transition == (KPIState.DETECTED, KPIState.OPEN)
    key = EvaluationKey.build("profit_margin", scope=SCOPE, at=T1230)
    scope_key = canonical_scope_key(SCOPE)
    assert store.get_observation(key) is not None
    assert store.get_committed_result(key) is not None
    record = store.get_state_record("profit_margin", scope_key)
    assert record.state == KPIState.OPEN
    assert record.version == 2
    assert record.last_evaluation_at == T1230
    assert [o.key.eval_at for o in store.get_history("profit_margin", scope_key)] == [T12, T1230]
    incident = store.find_active_incident("profit_margin", scope_key)
    assert incident is not None and incident.id == second.new_incidents[0].id
    assert [e.kind for e in store.list_pending_notifications()] == ["incident_opened"]


def test_restart_survival(store_kind, store_factory, monkeypatch):
    if store_kind == "memory":
        pytest.skip("memory store is process-local by design")
    first_store = store_factory()
    eng = engine(first_store)
    patch_anomaly(monkeypatch, eng)
    eng.process(metric="profit_margin", at=T12, scope=SCOPE)
    first_store.close()

    restarted = store_factory()
    eng2 = engine(restarted)
    calls = patch_anomaly(monkeypatch, eng2)
    again = eng2.process(metric="profit_margin", at=T12, scope=SCOPE)
    assert again.idempotent is True
    assert calls["evaluate"] == 0
    nxt = eng2.process(metric="profit_margin", at=T1230, scope=SCOPE)
    assert nxt.transition == (KPIState.DETECTED, KPIState.OPEN)
    cursor = restarted.latest_committed_evaluation("profit_margin", canonical_scope_key(SCOPE))
    assert cursor is not None and cursor.effective_at == T1230


def test_commit_failure_rolls_back_everything(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    patch_anomaly(monkeypatch, eng)
    eng.process(metric="profit_margin", at=T12, scope=SCOPE)

    def boom(tx):
        raise RuntimeError("injected commit failure")

    store.commit_hook = boom
    with pytest.raises(RuntimeError, match="injected"):
        eng.process(metric="profit_margin", at=T1230, scope=SCOPE)
    store.commit_hook = None

    key = EvaluationKey.build("profit_margin", scope=SCOPE, at=T1230)
    scope_key = canonical_scope_key(SCOPE)
    assert store.get_observation(key) is None
    assert store.get_committed_result(key) is None
    assert store.find_active_incident("profit_margin", scope_key) is None
    assert store.list_pending_notifications() == []
    assert store.get_state_record("profit_margin", scope_key).version == 1
    # Claim was released: the window can be processed again.
    retried = eng.process(metric="profit_margin", at=T1230, scope=SCOPE)
    assert retried.transition == (KPIState.DETECTED, KPIState.OPEN)


def test_duplicate_evaluation_is_idempotent(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    calls = patch_anomaly(monkeypatch, eng)
    first = eng.process(metric="profit_margin", at=T12, scope=SCOPE)
    second = eng.process(metric="profit_margin", at=T12, scope=SCOPE)
    assert first.idempotent is False
    assert second.idempotent is True
    assert calls["evaluate"] == 1
    assert len(store.get_history("profit_margin", canonical_scope_key(SCOPE))) == 1


def test_concurrent_claims_single_owner(store_kind, store_factory):
    stores = [store_factory() for _ in range(2 if store_kind == "postgres" else 1)]
    key = EvaluationKey.build("profit_margin", scope=SCOPE, at=T12)
    barrier = threading.Barrier(8)
    statuses: list[EvaluationClaimStatus] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait(timeout=5)
        claim = stores[i % len(stores)].claim_evaluation(key)
        with lock:
            statuses.append(claim.status)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert statuses.count(EvaluationClaimStatus.ACQUIRED) == 1
    assert statuses.count(EvaluationClaimStatus.IN_PROGRESS) == 7


def test_concurrent_workers_commit_once(store_kind, store_factory, monkeypatch):
    stores = [store_factory() for _ in range(2 if store_kind == "postgres" else 1)]
    engines = [engine(s) for s in stores]
    for e in engines:
        patch_anomaly(monkeypatch, e)
    barrier = threading.Barrier(4)
    outcomes: list[object] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait(timeout=5)
        try:
            result: object = engines[i % len(engines)].process(
                metric="profit_margin", at=T12, scope=SCOPE
            )
        except EvaluationInProgressError as exc:
            result = exc
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    committed = [o for o in outcomes if not isinstance(o, Exception) and not o.idempotent]
    assert len(committed) == 1
    assert len(stores[0].get_history("profit_margin", canonical_scope_key(SCOPE))) == 1


def test_ordered_streams_apply_in_effective_at_order(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    patch_anomaly(monkeypatch, eng)
    original = eng.evaluate
    both_claimed = threading.Barrier(3)
    release_early = threading.Event()

    def ordered_evaluate(name, at, filters=None, **kwargs):
        both_claimed.wait(timeout=5)
        if at == T12:
            assert release_early.wait(timeout=10)
        result = original(name, at, filters)
        if at == T1230:
            release_early.set()
        return result

    monkeypatch.setattr(eng, "evaluate", ordered_evaluate)
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def worker(label: str, at) -> None:
        try:
            results[label] = eng.process(metric="profit_margin", at=at, scope=SCOPE)
        except BaseException as exc:  # noqa: BLE001
            errors[label] = exc

    threads = [
        threading.Thread(target=worker, args=("a", T12)),
        threading.Thread(target=worker, args=("b", T1230)),
    ]
    for t in threads:
        t.start()
    both_claimed.wait(timeout=5)
    for t in threads:
        t.join(timeout=30)
    assert errors == {}, errors
    assert results["a"].transition == (KPIState.NORMAL, KPIState.DETECTED)
    assert results["b"].transition == (KPIState.DETECTED, KPIState.OPEN)


def test_stale_window_rejected(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    patch_anomaly(monkeypatch, eng)
    eng.process(metric="profit_margin", at=T1230, scope=SCOPE)
    with pytest.raises(StaleEvaluationError):
        eng.process(metric="profit_margin", at=T12, scope=SCOPE)
    key = EvaluationKey.build("profit_margin", scope=SCOPE, at=T12)
    assert store.get_committed_result(key) is None


def test_claim_lease_expiry_allows_takeover(store_factory):
    store = store_factory(claim_ttl=0.3)
    key = EvaluationKey.build("profit_margin", scope=SCOPE, at=T12)
    first = store.claim_evaluation(key)
    assert first.status == EvaluationClaimStatus.ACQUIRED
    assert store.claim_evaluation(key).status == EvaluationClaimStatus.IN_PROGRESS
    time.sleep(1.2)
    second = store.claim_evaluation(key)
    assert second.status == EvaluationClaimStatus.ACQUIRED
    assert second.token != first.token
    # The original owner lost its claim: its commit must not apply.
    with pytest.raises(EvaluationInProgressError):
        with store.transaction(evaluation_key=key, claim_token=first.token) as tx:
            tx.stage_observation(
                StoredObservation(
                    key=key,
                    status=status("profit_margin", T12, anomaly=False),
                    recorded_at=T12,
                )
            )
            tx.commit()
    assert store.get_observation(key) is None


def test_engine_retries_on_stream_commit_conflict(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    patch_anomaly(monkeypatch, eng)
    original_transaction = store.transaction
    calls = {"n": 0}

    def flaky_transaction(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StreamCommitConflict("injected conflict")
        return original_transaction(**kwargs)

    monkeypatch.setattr(store, "transaction", flaky_transaction)
    result = eng.process(metric="profit_margin", at=T12, scope=SCOPE)
    assert result.transition == (KPIState.NORMAL, KPIState.DETECTED)
    assert calls["n"] == 2


def test_engine_gives_up_after_bounded_conflicts(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    patch_anomaly(monkeypatch, eng)

    def always_conflict(**kwargs):
        raise StreamCommitConflict("injected conflict")

    monkeypatch.setattr(store, "transaction", always_conflict)
    with pytest.raises(StreamCommitConflict):
        eng.process(metric="profit_margin", at=T12, scope=SCOPE)
    key = EvaluationKey.build("profit_margin", scope=SCOPE, at=T12)
    assert store.get_committed_result(key) is None


def test_postgres_version_conflict_is_retried(store_kind, store_factory, monkeypatch):
    if store_kind == "memory":
        pytest.skip("memory store serializes the whole ordered section in-process")
    store = store_factory()
    eng = engine(store)
    patch_anomaly(monkeypatch, eng)
    eng.process(metric="profit_margin", at=T12, scope=SCOPE)
    scope_key = canonical_scope_key(SCOPE)
    bumped = {"done": False}

    def bump_then_impact(*args, **kwargs):
        # Inside the ordered section, after state was read: a concurrent
        # writer (e.g. acknowledge) moves the state version.
        if not bumped["done"]:
            bumped["done"] = True
            current = store.get_state_record("profit_margin", scope_key)
            store.set_state_record(current.model_copy(update={"version": current.version + 1}))
        return 120.0

    monkeypatch.setattr(eng, "estimate_impact_eur", bump_then_impact)
    result = eng.process(metric="profit_margin", at=T1230, scope=SCOPE)
    assert result.transition == (KPIState.DETECTED, KPIState.OPEN)
    assert store.get_state_record("profit_margin", scope_key).version == 3


def test_postgres_earlier_claim_at_commit_is_retried(store_kind, store_factory, monkeypatch):
    if store_kind == "memory":
        pytest.skip("memory store holds the stream for the whole ordered section")
    store = store_factory()
    eng = engine(store)
    patch_anomaly(monkeypatch, eng)
    earlier = EvaluationKey.build("profit_margin", scope=SCOPE, at=T12)
    injected = {"claim": None}

    def claim_earlier_then_impact(*args, **kwargs):
        if injected["claim"] is None:
            injected["claim"] = store.claim_evaluation(earlier)

            def release_later() -> None:
                time.sleep(0.3)
                store.release_evaluation_claim(earlier, token=injected["claim"].token)

            threading.Thread(target=release_later, daemon=True).start()
        return 120.0

    monkeypatch.setattr(eng, "estimate_impact_eur", claim_earlier_then_impact)
    result = eng.process(metric="profit_margin", at=T1230, scope=SCOPE)
    assert result.transition == (KPIState.NORMAL, KPIState.DETECTED)
    assert injected["claim"] is not None


def test_evaluation_cursor_tracks_latest_commit(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    patch_anomaly(monkeypatch, eng, anomaly=False)
    scope_key = canonical_scope_key(SCOPE)
    assert store.latest_committed_evaluation("profit_margin", scope_key) is None
    eng.process(metric="profit_margin", at=T12, scope=SCOPE)
    eng.process(metric="profit_margin", at=T13, scope=SCOPE)
    cursor = store.latest_committed_evaluation("profit_margin", scope_key)
    assert cursor is not None
    assert cursor.effective_at == T13
    assert store.latest_committed_evaluation("basket_cliff", scope_key) is None
