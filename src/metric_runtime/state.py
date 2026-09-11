"""Metric state machine.

Detection asks whether something is unusual.
State determines whether the organization should care yet.
An anomaly is not an alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from metric_runtime.models import (
    KPIState,
    KPIStateTransition,
    KPIStatus,
    QualityReport,
    StoredObservation,
)

if TYPE_CHECKING:
    from metric_runtime.engine import KPIEngine

# Re-export for public API stability.
__all__ = [
    "KPIStateTransition",
    "StatePolicy",
    "StateSignal",
    "collect_window_history",
    "evolve_state",
    "observation_to_detection",
]


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


@dataclass(frozen=True)
class StateSignal:
    """One history step visible to state evolution.

    Ineligible (quality-failed) windows reset consecutive detection streaks
    even though they are not treated as healthy normals.
    """

    detected: bool
    eligible: bool
    support_ok: bool = True


def observation_to_detection(status: KPIStatus) -> KPIState:
    """Detection answers: is this unusual?"""
    if status.anomaly and status.support_ok:
        return KPIState.DETECTED
    return KPIState.NORMAL


def signals_from_history(history: list[StoredObservation]) -> list[StateSignal]:
    """Convert stored observations into state-evolution signals."""
    signals: list[StateSignal] = []
    for item in history:
        detected = bool(item.status.anomaly and item.status.support_ok)
        signals.append(
            StateSignal(
                detected=detected and item.eligible_for_state,
                eligible=item.eligible_for_state,
                support_ok=item.status.support_ok,
            )
        )
    return signals


def _trailing_detection_streak(signals: list[StateSignal]) -> int:
    """Count consecutive trailing *eligible* detections.

    An ineligible window breaks the streak (does not bridge anomalies).
    """
    streak = 0
    for signal in reversed(signals):
        if not signal.eligible:
            break
        if signal.detected:
            streak += 1
            continue
        break
    return streak


def _trailing_healthy_streak(signals: list[StateSignal]) -> int:
    streak = 0
    for signal in reversed(signals):
        if not signal.eligible:
            break
        if not signal.detected:
            streak += 1
            continue
        break
    return streak


def evolve_state(
    history: list[KPIStatus] | list[StateSignal] | list[StoredObservation],
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
    - ineligible (bad-quality) windows break consecutive detection streaks
    """
    policy = policy or StatePolicy()
    previous = previous or KPIState.NORMAL

    if quality is not None and not quality.healthy:
        return KPIState.SUPPRESSED

    signals: list[StateSignal]
    if not history:
        signals = []
    elif isinstance(history[0], StateSignal):
        signals = list(history)  # type: ignore[arg-type]
    elif isinstance(history[0], StoredObservation):
        signals = signals_from_history(history)  # type: ignore[arg-type]
    else:
        # Legacy: list[KPIStatus] assumed fully eligible.
        statuses = [item for item in history if isinstance(item, KPIStatus)]
        signals = [
            StateSignal(
                detected=observation_to_detection(status) == KPIState.DETECTED,
                eligible=True,
                support_ok=status.support_ok,
            )
            for status in statuses
        ]

    if not signals:
        return KPIState.NORMAL

    healthy = _trailing_healthy_streak(signals)
    streak = _trailing_detection_streak(signals)
    last = signals[-1]

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
        if policy.require_support and not last.support_ok:
            return KPIState.DETECTED
        return KPIState.OPEN

    if streak == 0:
        if previous == KPIState.RESOLVED:
            return KPIState.RESOLVED
        if previous == KPIState.SUPPRESSED and not last.eligible:
            return KPIState.SUPPRESSED
        return KPIState.NORMAL
    if streak < policy.persistence:
        return KPIState.DETECTED
    if impact_eur < policy.min_impact_eur:
        return KPIState.DETECTED
    if policy.require_support and not last.support_ok:
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
