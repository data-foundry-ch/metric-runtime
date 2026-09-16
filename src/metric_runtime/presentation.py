"""Presentation policy helpers (not detectors, not UI).

Detectors and operational state remain authoritative for ops workflows.
These bands are optional product/display policy for embedders.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from metric_runtime.models import Directionality


class PresentationBand(str, Enum):
    """Directionality-aware display band for a measured value."""

    ON_TARGET = "on_target"
    AT_RISK = "at_risk"
    OFF_TARGET = "off_target"
    NO_DATA = "no_data"


class PresentationThreshold(BaseModel):
    """Optional target / warning / critical levels for presentation only.

    No colors, alerts, or delivery semantics — products map bands to UI.
    """

    target: float
    warning: float | None = None
    critical: float | None = None


def classify_presentation_band(
    value: float | None,
    *,
    thresholds: PresentationThreshold,
    directionality: Directionality = Directionality.TWO_SIDED,
) -> PresentationBand:
    """Map a measured value to a presentation band.

    ``None`` → ``no_data``. Otherwise bands depend on directionality:

    - ``LOWER_IS_BAD`` (higher is better): adverse direction is downward
    - ``HIGHER_IS_BAD`` (lower is better): adverse direction is upward
    - ``TWO_SIDED``: ``warning`` / ``critical`` are absolute deviations from
      ``target``

    Missing warning/critical collapses the unused middle band.
    """
    if value is None:
        return PresentationBand.NO_DATA

    target = thresholds.target
    warning = thresholds.warning
    critical = thresholds.critical

    if directionality == Directionality.LOWER_IS_BAD:
        return _band_higher_is_better(value, target=target, warning=warning, critical=critical)
    if directionality == Directionality.HIGHER_IS_BAD:
        return _band_lower_is_better(value, target=target, warning=warning, critical=critical)
    return _band_two_sided(value, target=target, warning=warning, critical=critical)


def _band_higher_is_better(
    value: float,
    *,
    target: float,
    warning: float | None,
    critical: float | None,
) -> PresentationBand:
    # Prefer warning as the on-target floor when present; else target.
    # Typical ordering: critical <= warning <= target.
    floor_ok = warning if warning is not None else target
    if value >= floor_ok:
        return PresentationBand.ON_TARGET
    if critical is not None and value >= critical:
        return PresentationBand.AT_RISK
    return PresentationBand.OFF_TARGET


def _band_lower_is_better(
    value: float,
    *,
    target: float,
    warning: float | None,
    critical: float | None,
) -> PresentationBand:
    # Prefer warning as the on-target ceiling when present; else target.
    # Typical ordering: target <= warning <= critical.
    ceil_ok = warning if warning is not None else target
    if value <= ceil_ok:
        return PresentationBand.ON_TARGET
    if critical is not None and value <= critical:
        return PresentationBand.AT_RISK
    return PresentationBand.OFF_TARGET


def _band_two_sided(
    value: float,
    *,
    target: float,
    warning: float | None,
    critical: float | None,
) -> PresentationBand:
    delta = abs(value - target)
    if warning is None and critical is None:
        return PresentationBand.ON_TARGET if delta == 0 else PresentationBand.OFF_TARGET
    warn = warning if warning is not None else 0.0
    if delta <= warn:
        return PresentationBand.ON_TARGET
    if critical is not None and delta <= critical:
        return PresentationBand.AT_RISK
    return PresentationBand.OFF_TARGET if critical is not None else PresentationBand.AT_RISK


__all__ = [
    "PresentationBand",
    "PresentationThreshold",
    "classify_presentation_band",
]
