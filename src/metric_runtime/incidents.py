"""Incident creation — separate from notification."""

from __future__ import annotations

from datetime import datetime, timedelta

from metric_runtime.investigation import investigate_metric, preferred_explanatory_path
from metric_runtime.models import Incident, IncidentState, InvestigationResult, QualityReport
from metric_runtime.ownership import resolve_alert_owner


def incident_from_investigation(
    engine,
    *,
    center_kpi: str,
    scope: dict[str, str],
    at: datetime,
    inv: InvestigationResult,
    state: IncidentState,
    first_detected: str,
    estimated_impact: float,
    persistence_windows: int = 1,
    context: list[str] | None = None,
    preferred_leaves: tuple[str, ...] = (),
) -> Incident:
    """Build an incident from an investigation result (no store I/O)."""
    primary = inv.primary_explanatory
    owner = resolve_alert_owner(engine.catalog_dict, primary, center_kpi)
    path = preferred_explanatory_path(inv, preferred_leaves=preferred_leaves)
    explanatory = primary.name if primary else center_kpi
    suppressed = [n for n in path if n != explanatory]
    roots = [c.name for c in inv.deepest_candidates]
    opened = at.isoformat(sep=" ") if state == IncidentState.OPEN else None
    return Incident(
        primary_metric=center_kpi,
        explanatory_kpi=explanatory,
        root_candidates=roots,
        scope=scope,
        owner=owner,
        state=state,
        opened_at=opened,
        updated_at=at.isoformat(sep=" "),
        first_detected=first_detected,
        estimated_impact=max(estimated_impact, inv.impact_eur),
        evidence=list(path),
        related_metrics=list(suppressed),
        supporting_metrics=list(suppressed),
        context=list(context or []),
        persistence_windows=persistence_windows,
        suppressed_ancestors=list(suppressed),
    )


def open_smart_incident(
    engine,
    *,
    center_kpi: str,
    scope: dict[str, str] | None = None,
    start: datetime,
    windows: int = 12,
    interval_minutes: int = 30,
    persistence: int = 2,
    min_impact_eur: float = 50.0,
    quality: QualityReport | None = None,
    preferred_leaves: tuple[str, ...] = (),
    context: list[str] | None = None,
) -> Incident | None:
    """
    detector → state → graph traversal → deepest explanatory KPI → owner.

    One incident for the chain; ancestors listed as suppressed supporting evidence.

    ``center_kpi`` is required — domain defaults belong in the example layer.
    """
    if quality is not None and not quality.healthy:
        return None

    scope = dict(scope or {})
    consecutive = 0
    first_detected: datetime | None = None
    last_impact = 0.0
    open_at: datetime | None = None

    for i in range(windows):
        at = start + timedelta(minutes=interval_minutes * i)
        status = engine.evaluate(center_kpi, at, scope)
        impact = engine.estimate_impact_eur(
            center_kpi, at, status.value, status.baseline_mean, scope
        )
        if status.anomaly and status.support_ok and impact >= min_impact_eur:
            consecutive += 1
            if first_detected is None:
                first_detected = at
            last_impact = impact
            if consecutive >= persistence:
                open_at = at
                break
        else:
            consecutive = 0
            first_detected = None

    if first_detected is None:
        return None

    investigate_at = open_at or first_detected
    inv = investigate_metric(
        engine,
        center_kpi,
        investigate_at,
        scope,
        preferred_leaves=preferred_leaves,
    )
    state = IncidentState.OPEN if open_at is not None else IncidentState.DETECTED
    return incident_from_investigation(
        engine,
        center_kpi=center_kpi,
        scope=scope,
        at=investigate_at,
        inv=inv,
        state=state,
        first_detected=first_detected.isoformat(sep=" "),
        estimated_impact=last_impact,
        persistence_windows=consecutive,
        context=context,
        preferred_leaves=preferred_leaves,
    )


def evaluate_watcher(
    engine,
    *,
    kpi: str,
    scope: dict[str, str] | None = None,
    start: datetime,
    interval_minutes: int = 30,
    windows: int = 48,
    persistence: int = 2,
    min_impact_eur: float = 50.0,
    quality: QualityReport | None = None,
    preferred_leaves: tuple[str, ...] = (),
    context: list[str] | None = None,
) -> Incident | None:
    """Back-compat wrapper around graph-aware incident opener."""
    return open_smart_incident(
        engine,
        center_kpi=kpi,
        scope=scope,
        start=start,
        windows=windows,
        interval_minutes=interval_minutes,
        persistence=persistence,
        min_impact_eur=min_impact_eur,
        quality=quality,
        preferred_leaves=preferred_leaves,
        context=context,
    )
