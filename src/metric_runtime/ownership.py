"""Ownership helpers — route to the team best positioned to act."""

from __future__ import annotations

from metric_runtime.models import KPI, ExplanatoryCandidate


def owner_for_metric(catalog: dict[str, KPI], name: str) -> str:
    return catalog[name].owner


def resolve_alert_owner(
    catalog: dict[str, KPI],
    primary: ExplanatoryCandidate | None,
    fallback_kpi: str,
) -> str:
    """
    Don't alert the owner of the red top-level number.
    Alert the owner of the lowest metric that explains why it turned red.
    """
    if primary is not None:
        return primary.owner
    return catalog[fallback_kpi].owner
