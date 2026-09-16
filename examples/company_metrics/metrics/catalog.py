"""Compose the company metrics catalog."""

from __future__ import annotations

from metric_runtime import MetricCatalog

from .finance import METRICS as FINANCE_METRICS
from .sales import METRICS as SALES_METRICS

catalog = MetricCatalog(
    [
        *FINANCE_METRICS,
        *SALES_METRICS,
    ],
    name="company_metrics",
)

__all__ = ["catalog"]
