"""Pure scheduling helpers."""

from __future__ import annotations

from datetime import timedelta

import pytest

from _runtime_helpers import ts
from metric_runtime.scheduling import align_tick, due_ticks, latest_due_tick, next_due_at

M15 = timedelta(minutes=15)
M30 = timedelta(minutes=30)


def test_align_tick_floors_to_interval():
    assert align_tick(ts(2026, 5, 15, 12, 7, 59), M15) == ts(2026, 5, 15, 12, 0)
    assert align_tick(ts(2026, 5, 15, 12, 15), M15) == ts(2026, 5, 15, 12, 15)
    assert align_tick(ts(2026, 5, 15, 23, 59), timedelta(days=1)) == ts(2026, 5, 15)


def test_align_tick_rejects_non_positive_interval():
    with pytest.raises(ValueError):
        align_tick(ts(2026, 5, 15), timedelta(0))


def test_window_is_due_only_after_it_closes_plus_lag():
    now = ts(2026, 5, 15, 13, 0)
    assert latest_due_tick(now, M30) == ts(2026, 5, 15, 12, 30)
    assert latest_due_tick(now - timedelta(seconds=1), M30) == ts(2026, 5, 15, 12, 0)
    assert latest_due_tick(now, M30, timedelta(minutes=5)) == ts(2026, 5, 15, 12, 0)
    assert next_due_at(now, M30) == ts(2026, 5, 15, 13, 30)
    assert next_due_at(now, M30, timedelta(minutes=5)) == ts(2026, 5, 15, 13, 5)


def test_never_evaluated_metric_only_considers_recent_windows():
    now = ts(2026, 5, 15, 13, 0)
    assert due_ticks(None, now, M15).ticks == [ts(2026, 5, 15, 12, 45)]
    result = due_ticks(None, now, M15, max_catchup=3)
    assert result.ticks == [
        ts(2026, 5, 15, 12, 15),
        ts(2026, 5, 15, 12, 30),
        ts(2026, 5, 15, 12, 45),
    ]
    assert result.skipped_count == 0


def test_ticks_after_cursor_are_due_in_order():
    now = ts(2026, 5, 15, 13, 0)
    cursor = ts(2026, 5, 15, 12, 15)
    result = due_ticks(cursor, now, M15, max_catchup=5)
    assert result.ticks == [ts(2026, 5, 15, 12, 30), ts(2026, 5, 15, 12, 45)]


def test_nothing_due_when_cursor_is_current():
    now = ts(2026, 5, 15, 13, 0)
    assert due_ticks(ts(2026, 5, 15, 12, 45), now, M15).ticks == []


def test_catchup_cap_skips_oldest_windows():
    now = ts(2026, 5, 15, 13, 0)
    cursor = ts(2026, 5, 15, 10, 0)
    result = due_ticks(cursor, now, M15, max_catchup=2)
    assert result.ticks == [ts(2026, 5, 15, 12, 30), ts(2026, 5, 15, 12, 45)]
    assert result.skipped_count == 9
    assert result.skipped_first == ts(2026, 5, 15, 10, 15)
    assert result.skipped_last == ts(2026, 5, 15, 12, 15)


def test_long_outage_does_not_materialize_skipped_windows():
    result = due_ticks(ts(2020, 1, 1), ts(2026, 5, 15), timedelta(minutes=1))
    assert len(result.ticks) == 1
    assert result.skipped_count > 3_000_000


def test_unaligned_cursor_resumes_on_next_tick():
    result = due_ticks(ts(2026, 5, 15, 12, 7), ts(2026, 5, 15, 13, 0), M15, max_catchup=10)
    assert result.ticks[0] == ts(2026, 5, 15, 12, 15)
