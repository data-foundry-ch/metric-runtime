"""First-class KPI calculation specifications.

A KPI defines business meaning. A Calculation defines how its value is obtained.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from metric_runtime.measures import Formula


def _validate_scalar_bindings(value: dict[str, Any]) -> dict[str, Any]:
    """Keep bindings JSON-serializable scalars (no secrets / nested objects)."""
    if not isinstance(value, dict):
        raise TypeError("bindings must be a dict")
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key).strip()
        if not name:
            raise ValueError("bindings keys must be non-empty strings")
        if isinstance(item, bool) or item is None or isinstance(item, (str, int, float, datetime)):
            cleaned[name] = item
            continue
        raise ValueError(
            f"bindings[{name!r}] must be a scalar "
            f"(str|int|float|bool|None|datetime), got {type(item)!r}"
        )
    return cleaned


class FormulaCalculation(BaseModel):
    """Aggregate a known measure (or ratio/difference) from a fact table."""

    kind: Literal["formula"] = "formula"
    formula: Formula


class SqlCalculation(BaseModel):
    """Parameterized scalar SQL producing one KPI value.

    The query must return exactly one row with a ``value`` column (or the
    column named by ``value_column``). Metric Runtime does **not** transpile
    SQL across warehouses — ``dialect`` selects a compatible executor.

    ``bindings`` are KPI-local default parameters. Request
    ``EvaluationContext`` bindings override these on key conflicts.
    """

    kind: Literal["sql"] = "sql"
    query: str
    dialect: str | None = None
    value_column: str = "value"
    bindings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("query")
    @classmethod
    def _non_empty_query(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("SqlCalculation.query must be a non-empty SQL string")
        return text

    @field_validator("bindings")
    @classmethod
    def _scalar_bindings(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_scalar_bindings(value)


class BatchCalculation(BaseModel):
    """Consume one named result from a shared batch source.

    Batch is an *execution* optimization, not a business dependency.
    """

    kind: Literal["batch"] = "batch"
    source: str
    result: str | None = None

    @field_validator("source")
    @classmethod
    def _non_empty_source(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("BatchCalculation.source must be non-empty")
        return text


class DerivedCalculation(BaseModel):
    """Compute a KPI from already-evaluated dependency observations."""

    kind: Literal["derived"] = "derived"
    expression: str

    @field_validator("expression")
    @classmethod
    def _non_empty_expression(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("DerivedCalculation.expression must be non-empty")
        return text


Calculation = Annotated[
    FormulaCalculation | SqlCalculation | BatchCalculation | DerivedCalculation,
    Field(discriminator="kind"),
]

_CALCULATION_ADAPTER: TypeAdapter[
    FormulaCalculation | SqlCalculation | BatchCalculation | DerivedCalculation
] = TypeAdapter(Calculation)


def parse_calculation(
    value: Any,
) -> FormulaCalculation | SqlCalculation | BatchCalculation | DerivedCalculation:
    """Parse a calculation dict/model (raises on invalid input)."""
    if isinstance(
        value, (FormulaCalculation, SqlCalculation, BatchCalculation, DerivedCalculation)
    ):
        return value
    if isinstance(value, Formula):
        return FormulaCalculation(formula=value)
    if isinstance(value, dict):
        payload = dict(value)
        if "kind" not in payload and "formula" in payload:
            payload["kind"] = "formula"
        return _CALCULATION_ADAPTER.validate_python(payload)
    raise TypeError(f"Cannot parse calculation from {type(value)!r}")


def coerce_calculation(
    value: Any,
) -> FormulaCalculation | SqlCalculation | BatchCalculation | DerivedCalculation | None:
    """Normalize dict / Formula / Calculation into a Calculation model."""
    if value is None:
        return None
    return parse_calculation(value)


def merge_parameter_bindings(
    context_bindings: dict[str, Any],
    local_bindings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge KPI/batch-local defaults with request bindings.

    Precedence: request (``EvaluationContext``) overrides local defaults.
    """
    merged = dict(local_bindings or {})
    merged.update(context_bindings)
    return merged


__all__ = [
    "BatchCalculation",
    "Calculation",
    "DerivedCalculation",
    "FormulaCalculation",
    "SqlCalculation",
    "coerce_calculation",
    "merge_parameter_bindings",
    "parse_calculation",
]
