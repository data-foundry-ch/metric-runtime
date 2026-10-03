"""Pure scheduling helpers (no I/O).

A *tick* is the ``at`` timestamp of one evaluation window: windows start on
multiples of the interval since the UTC epoch. Tick ``T`` covers
``[T, T + interval)`` and becomes due once ``now >= T + interval + lag``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from metric_runtime.identity import ensure_utc

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _require_positive(interval: timedelta) -> None:
    if interval <= timedelta(0):
        raise ValueError(f"interval must be positive, got {interval!r}")


def align_tick(moment: datetime, interval: timedelta) -> datetime:
    """Floor ``moment`` to a multiple of ``interval`` since the UTC epoch."""
    _require_positive(interval)
    moment = ensure_utc(moment)
    steps = (moment - _EPOCH) // interval
    return _EPOCH + steps * interval


def latest_due_tick(now: datetime, interval: timedelta, lag: timedelta = timedelta(0)) -> datetime:
    """Most recent tick whose window has closed (plus ``lag``) at ``now``."""
    return align_tick(ensure_utc(now) - lag, interval) - interval


def next_due_at(now: datetime, interval: timedelta, lag: timedelta = timedelta(0)) -> datetime:
    """Wall-clock time at which the next not-yet-due tick becomes due."""
    return latest_due_tick(now, interval, lag) + 2 * interval + lag


@dataclass(frozen=True)
class DueTicks:
    """Ticks to evaluate (ascending) plus the range skipped by the catch-up cap."""

    ticks: list[datetime] = field(default_factory=list)
    skipped_count: int = 0
    skipped_first: datetime | None = None
    skipped_last: datetime | None = None


def due_ticks(
    cursor: datetime | None,
    now: datetime,
    interval: timedelta,
    *,
    lag: timedelta = timedelta(0),
    max_catchup: int = 1,
) -> DueTicks:
    """Ascending due ticks strictly after ``cursor``, capped at ``max_catchup``.

    ``cursor`` is the ``effective_at`` of the latest committed evaluation (or
    ``None`` for a never-evaluated metric, which only considers the most
    recent ``max_catchup`` windows instead of backfilling history).
    """
    _require_positive(interval)
    if max_catchup < 1:
        raise ValueError("max_catchup must be >= 1")
    latest = latest_due_tick(now, interval, lag)
    if cursor is None:
        first = latest - (max_catchup - 1) * interval
    else:
        first = align_tick(cursor, interval) + interval
    if first > latest:
        return DueTicks()
    count = (latest - first) // interval + 1
    keep_from = max(0, count - max_catchup)
    ticks = [first + i * interval for i in range(keep_from, count)]
    if keep_from == 0:
        return DueTicks(ticks=ticks)
    return DueTicks(
        ticks=ticks,
        skipped_count=keep_from,
        skipped_first=first,
        skipped_last=first + (keep_from - 1) * interval,
    )
