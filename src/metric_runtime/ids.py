"""Stable metric identity rules.

IDs are machine identity. Display names are presentation.
Prefer identifiers compatible with derived-expression references.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BeforeValidator

from metric_runtime.exceptions import InvalidMetricDefinitionError

METRIC_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_metric_id(value: Any) -> str:
    """Validate and normalize a Metric.id."""
    if not isinstance(value, str):
        raise InvalidMetricDefinitionError(f"Metric.id must be a string, got {type(value)!r}")
    text = value.strip()
    if not METRIC_ID_PATTERN.match(text):
        raise InvalidMetricDefinitionError(
            f"Invalid Metric.id {value!r}. "
            "Expected pattern ^[a-z][a-z0-9_]*$ "
            "(e.g. 'profit_margin', 'closed_won_revenue')."
        )
    return text


MetricId = Annotated[str, BeforeValidator(validate_metric_id)]


__all__ = ["METRIC_ID_PATTERN", "MetricId", "validate_metric_id"]
