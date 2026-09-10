"""Metric state machine.

Detection asks whether something is unusual.
State determines whether the organization should care yet.
An anomaly is not an alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from metric_runtime.models import KPIState, KPIStatus, QualityReport

if TYPE_CHECKING:
    from metric_runtime.engine import KPIEngine


@dataclass
class StatePolicy:
    """Policy for Observation → Detection → OPEN transitions."""

    persistence: int = 2
    detections_before_open: int | None = None
    resolve_after_healthy_windows: int = 2
    cooldown_minutes: int = 30
    min_impact_eur: float = 50.0
    require_support: bool = True

    def __post_init__(self) -> None:
        if self.detections_before_open is not None:
            self.persistence = self.detections_before_open


def observation_to_detection(status: KPIStatus) -> KPIState:
    """Detection answers: is this unusual?"""
    if status.anomaly and status.support_ok:
        return KPIState.DETECTED
    return KPIState.NORMAL


def evolve_state(
    history: list[KPIStatus],
    *,
    policy: StatePolicy | None = None,
    impact_eur: float = 0.0,
    quality: QualityReport | None = None,
) -> KPIState:
    """
    Observation → Detection → OPEN when persistence + impact + quality hold.

    An anomaly is not an alert.
    """
    policy = policy or StatePolicy()
    if quality is not None and not quality.healthy:
        return KPIState.SUPPRESSED

    if not history:
        return KPIState.NORMAL

    streak = 0
    for status in reversed(history):
        if observation_to_detection(status) == KPIState.DETECTED:
            streak += 1
        else:
            break

    if streak == 0:
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
