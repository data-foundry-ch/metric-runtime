"""Generic measure references and Formula shapes."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator

_MEASURE_REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def coerce_measure_ref(value: Any) -> str:
    """Normalize enums / strings into a generic measure identifier."""
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str):
        raise TypeError(f"Measure reference must be a string, got {type(value)!r}")
    name = value.strip()
    if not _MEASURE_REF_RE.match(name):
        raise ValueError(
            f"Invalid measure reference {name!r}. "
            "Expected an identifier like 'orders' or 'gross_revenue'."
        )
    return name


# Public alias: validated measure identifier (not a framework-owned domain enum).
MeasureRef = str


class Formula(BaseModel):
    """How a formula calculation aggregates generic measure references."""

    kind: Literal["sum", "ratio", "difference"]
    measure: MeasureRef | None = None
    numerator: MeasureRef | None = None
    denominator: MeasureRef | None = None
    left: MeasureRef | None = None
    right: MeasureRef | None = None

    @field_validator("measure", "numerator", "denominator", "left", "right", mode="before")
    @classmethod
    def _coerce_refs(cls, value: Any) -> Any:
        if value is None:
            return None
        return coerce_measure_ref(value)

    @model_validator(mode="after")
    def validate_shape(self) -> Formula:
        if self.kind == "sum" and self.measure is None:
            raise ValueError("sum formulas require measure")
        if self.kind == "ratio" and (self.numerator is None or self.denominator is None):
            raise ValueError("ratio formulas require numerator and denominator")
        if self.kind == "difference" and (self.left is None or self.right is None):
            raise ValueError("difference formulas require left and right")
        return self

    @classmethod
    def sum(cls, measure: str | Enum) -> Formula:
        return cls(kind="sum", measure=coerce_measure_ref(measure))

    @classmethod
    def ratio(cls, numerator: str | Enum, denominator: str | Enum) -> Formula:
        return cls(
            kind="ratio",
            numerator=coerce_measure_ref(numerator),
            denominator=coerce_measure_ref(denominator),
        )

    @classmethod
    def difference(cls, left: str | Enum, right: str | Enum) -> Formula:
        return cls(
            kind="difference",
            left=coerce_measure_ref(left),
            right=coerce_measure_ref(right),
        )


__all__ = ["Formula", "MeasureRef", "coerce_measure_ref"]
