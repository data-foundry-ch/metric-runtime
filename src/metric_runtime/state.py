"""Metric state machine.

Detection asks whether something is unusual.
State determines whether the organization should care yet.
An anomaly is not an alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

from metric_runtime.models import KPIState, KPIStatus, QualityReport

if TYPE_CHECKING:
    from metric_runtime.engine import KPIEngine


@dataclass
class StatePolicy:
    """Policy for Observation → Detection → OPEN / RESOLVED transitions."""

    persistence: int = 2
    detections_before_open: int | None = None
    resolve_after_healthy_windows: int = 2
    cooldown_minutes: int = 30
    min_impact_eur: float = 50.0
    require_support: bool = True

    def __post_init__(self) -> None:
        if self.detections_before_open is not None:
            self.persistence = self.detections_before_open


class KPIStateTransition(NamedTuple):
    """Previous → current operational state for one process tick."""

    previous: KPIState
    current: KPIState


def observation_to_detection(status: KPIStatus) -> KPIState:
    """Detection answers: is this unusual?"""
    if status.anomaly and status.support_ok:
        return KPIState.DETECTED
    return KPIState.NORMAL


def _trailing_detection_streak(history: list[KPIStatus]) -> int:
    streak = 0
    for status in reversed(history):
        if observation_to_detection(status) == KPIState.DETECTED:
            streak += 1
        else:
            break
    return streak


def _trailing_healthy_streak(history: list[KPIStatus]) -> int:
    streak = 0
    for status in reversed(history):
        if observation_to_detection(status) == KPIState.NORMAL:
            streak += 1
        else:
            break
    return streak


def evolve_state(
    history: list[KPIStatus],
    *,
    policy: StatePolicy | None = None,
    impact_eur: float = 0.0,
    quality: QualityReport | None = None,
    previous: KPIState | None = None,
) -> KPIState:
    """
    Observation → Detection → OPEN when persistence + impact + quality hold.

    Also:
    - unhealthy quality → SUPPRESSED
    - enough healthy windows after OPEN/ACK → RESOLVED
    - ACKNOWLEDGED is sticky while the anomaly persists

    Callers must pass only state-eligible observations in ``history``.
    """
    policy = policy or StatePolicy()
    previous = previous or KPIState.NORMAL

    if quality is not None and not quality.healthy:
        return KPIState.SUPPRESSED

    if not history:
        return KPIState.NORMAL

    healthy = _trailing_healthy_streak(history)
    streak = _trailing_detection_streak(history)

    if previous == KPIState.ACKNOWLEDGED and streak > 0:
        return KPIState.ACKNOWLEDGED

    if previous in {KPIState.OPEN, KPIState.ACKNOWLEDGED} and healthy > 0:
        if healthy >= policy.resolve_after_healthy_windows:
            return KPIState.RESOLVED
        return previous

    if previous == KPIState.DETECTED and healthy > 0:
        return KPIState.NORMAL

    if previous == KPIState.RESOLVED and streak > 0:
        if streak < policy.persistence:
            return KPIState.DETECTED
        if impact_eur < policy.min_impact_eur:
            return KPIState.DETECTED
        if policy.require_support and not history[-1].support_ok:
            return KPIState.DETECTED
        return KPIState.OPEN

    if streak == 0:
        if previous == KPIState.RESOLVED:
            return KPIState.RESOLVED
        return KPIState.NORMAL
    if streak < policy.persistence:
        return KPIState.DETECTED
    if impact_eur < policy.min_impact_eur:
        return KPIState.DETECTED
    if policy.require_support and not history[-1].support_ok:
        return KPIState.DETECTED
    return KPIState.OPEN


def collect_window_history(
    engine: KPIEngine,
    kpi: str,
    start: datetime,
    *,
    scope: dict[str, str] | None = None,
    windows: int = 8,
    interval_minutes: int = 30,
) -> list[KPIStatus]:
    history: list[KPIStatus] = []
    for i in range(windows):
        at = start + timedelta(minutes=interval_minutes * i)
        history.append(engine.evaluate(kpi, at, scope))
    return history
