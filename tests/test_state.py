"""State machine tests."""

from __future__ import annotations

from metric_runtime.models import Directionality, KPIState, KPIStatus, QualityReport
from metric_runtime.state import StatePolicy, evolve_state


def _anom() -> KPIStatus:
    return KPIStatus(
        name="m",
        value=1.0,
        baseline_mean=2.0,
        baseline_std=0.1,
        z_score=-5.0,
        relative_change=-0.5,
        anomaly=True,
        support=100,
        support_ok=True,
        as_of="t",
        directionality=Directionality.LOWER_IS_BAD,
        state=KPIState.DETECTED,
        severity=5.0,
    )


def test_state_requires_persistence():
    st = _anom()
    one = evolve_state([st], policy=StatePolicy(persistence=2, min_impact_eur=1), impact_eur=100)
    two = evolve_state(
        [st, st],
        policy=StatePolicy(persistence=2, min_impact_eur=1),
        impact_eur=100,
    )
    assert one == KPIState.DETECTED
    assert two == KPIState.OPEN


def test_quality_suppresses():
    bad = QualityReport(
        healthy=False,
        freshness_ok=False,
        completeness_ok=True,
        volume_ok=True,
        row_count=0,
        missing_rate=0.0,
        latest_ts="",
        message="stale",
    )
    assert evolve_state([_anom(), _anom()], impact_eur=100, quality=bad) == KPIState.SUPPRESSED


def _healthy() -> KPIStatus:
    return KPIStatus(
        name="m",
        value=2.0,
        baseline_mean=2.0,
        baseline_std=0.1,
        z_score=0.1,
        relative_change=0.0,
        anomaly=False,
        support=100,
        support_ok=True,
        as_of="t",
        directionality=Directionality.LOWER_IS_BAD,
        state=KPIState.NORMAL,
        severity=0.0,
    )


def test_open_resolves_after_healthy_windows():
    policy = StatePolicy(persistence=2, min_impact_eur=1, resolve_after_healthy_windows=2)
    opened = evolve_state(
        [_anom(), _anom()],
        policy=policy,
        impact_eur=100,
        previous=KPIState.DETECTED,
    )
    assert opened == KPIState.OPEN
    holding = evolve_state(
        [_anom(), _anom(), _healthy()],
        policy=policy,
        impact_eur=0,
        previous=KPIState.OPEN,
    )
    assert holding == KPIState.OPEN
    resolved = evolve_state(
        [_anom(), _anom(), _healthy(), _healthy()],
        policy=policy,
        impact_eur=0,
        previous=KPIState.OPEN,
    )
    assert resolved == KPIState.RESOLVED
