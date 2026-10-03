"""NO_DATA is a committed evaluation outcome; execution errors are not."""

from __future__ import annotations

from datetime import timedelta

import pytest

from _runtime_helpers import engine, patch_script, ts
from metric_runtime import KPIState
from metric_runtime.exceptions import MetricRuntimeError, NoDataError
from metric_runtime.identity import EvaluationKey, canonical_scope_key
from metric_runtime.models import StoredObservation
from metric_runtime.state import signals_from_history

T0 = ts(2026, 5, 15, 12, 0)
HOUR = timedelta(hours=1)
UNSCOPED = canonical_scope_key()


def test_no_data_is_committed_without_state_change_or_incidents(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    calls = patch_script(monkeypatch, eng, {T0: "no_data"})

    result = eng.process("profit_margin", at=T0)

    assert result.status.is_no_data
    assert result.transition.previous == result.transition.current == KPIState.NORMAL
    assert result.new_incidents == [] and result.notifications == []
    record = store.get_committed_result(EvaluationKey.build("profit_margin", at=T0))
    assert record is not None
    assert record.observation.is_no_data
    assert record.observation.no_data_reason == "no_data"
    assert record.observation.eligible_for_state is False
    assert store.latest_committed_evaluation("profit_margin").effective_at == T0
    assert store.list_pending_notifications() == []
    state = store.get_state_record("profit_margin", UNSCOPED)
    assert state.state == KPIState.NORMAL
    assert state.last_evaluation_at == T0

    # Re-processing is idempotent: no second calculation.
    again = eng.process("profit_margin", at=T0)
    assert again.idempotent is True
    assert len(calls) == 1


def test_open_incident_stays_open_across_no_data_gap(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    script = {T0: "anomaly", T0 + HOUR: "anomaly", T0 + 2 * HOUR: "no_data"}
    patch_script(monkeypatch, eng, script)

    eng.process("profit_margin", at=T0)
    opened = eng.process("profit_margin", at=T0 + HOUR)
    assert opened.transition.current == KPIState.OPEN
    incident_id = opened.new_incidents[0].id
    pending_before = len(store.list_pending_notifications())

    gap = eng.process("profit_margin", at=T0 + 2 * HOUR)
    assert gap.transition.previous == gap.transition.current == KPIState.OPEN
    assert gap.notifications == [] and gap.updated_incidents == []
    assert len(store.list_pending_notifications()) == pending_before
    active = store.find_active_incident("profit_margin", UNSCOPED)
    assert active is not None and active.id == incident_id
    assert store.get_state_record("profit_margin", UNSCOPED).state == KPIState.OPEN


def test_no_data_gap_does_not_reset_detection_streak(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    script = {T0: "anomaly", T0 + HOUR: "no_data", T0 + 2 * HOUR: "anomaly"}
    patch_script(monkeypatch, eng, script)

    first = eng.process("profit_margin", at=T0)
    assert first.transition.current == KPIState.DETECTED
    eng.process("profit_margin", at=T0 + HOUR)
    third = eng.process("profit_margin", at=T0 + 2 * HOUR)
    # persistence=2: the two anomalous windows around the gap open the incident.
    assert third.transition.current == KPIState.OPEN


def test_signals_from_history_skips_no_data():
    from _runtime_helpers import status

    key = EvaluationKey.build("profit_margin", at=T0)
    obs = [
        StoredObservation(
            key=key, status=status("profit_margin", T0, anomaly=True), recorded_at=T0
        ),
        StoredObservation(
            key=EvaluationKey.build("profit_margin", at=T0 + HOUR),
            status=status("profit_margin", T0 + HOUR, anomaly=False),
            recorded_at=T0,
            value_status="no_data",
            no_data_reason="no_data",
        ),
    ]
    assert len(signals_from_history(obs)) == 1


def test_execution_error_commits_nothing_and_can_be_retried(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)
    outcomes = {T0: "error"}
    patch_script(monkeypatch, eng, outcomes)

    with pytest.raises(MetricRuntimeError):
        eng.process("profit_margin", at=T0)
    key = EvaluationKey.build("profit_margin", at=T0)
    assert store.get_committed_result(key) is None
    assert store.latest_committed_evaluation("profit_margin") is None
    assert store.get_state_record("profit_margin", UNSCOPED).version == 0

    outcomes[T0] = "healthy"
    result = eng.process("profit_margin", at=T0)
    assert result.idempotent is False
    assert store.latest_committed_evaluation("profit_margin").effective_at == T0


def test_no_baseline_reason_is_recorded(store_factory, monkeypatch):
    store = store_factory()
    eng = engine(store)

    def fake_evaluate(name, at, filters=None, **kwargs):
        raise NoDataError("no baseline", reason="no_baseline")

    monkeypatch.setattr(eng, "evaluate", fake_evaluate)
    result = eng.process("profit_margin", at=T0)
    assert result.evaluation_record is not None
    assert result.evaluation_record.observation.no_data_reason == "no_baseline"
