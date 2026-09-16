"""Extensible unit descriptions (semantic, not dimensional algebra)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator


class UnitSpec(BaseModel):
    """Lightweight unit description for embedders and catalogs.

    No conversion / Pint-style algebra — descriptive only.
    """

    id: str
    symbol: str | None = None
    description: str | None = None

    @field_validator("id")
    @classmethod
    def _non_empty_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("UnitSpec.id must be non-empty")
        return text

    def __str__(self) -> str:
        return self.id

    @classmethod
    def coerce(cls, value: Any) -> UnitSpec:
        """Accept ``UnitSpec``, ``{\"id\": ...}``, or a plain string id."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(id=value)
        if isinstance(value, dict):
            return cls.model_validate(value)
        raise TypeError(f"Cannot coerce unit from {type(value)!r}")


# Common built-in aliases authors may still pass as plain strings.
_LEGACY_UNIT_ALIASES = {
    "count": UnitSpec(id="count"),
    "ratio": UnitSpec(id="ratio"),
    "eur": UnitSpec(id="EUR", symbol="€"),
    "EUR": UnitSpec(id="EUR", symbol="€"),
    "percent": UnitSpec(id="percent", symbol="%"),
    "unit": UnitSpec(id="unit"),
}


def coerce_unit(value: Any) -> UnitSpec:
    """Normalize authoring strings / dicts / UnitSpec into UnitSpec."""
    if value is None:
        return UnitSpec(id="unit")
    if isinstance(value, str):
        key = value.strip()
        if key in _LEGACY_UNIT_ALIASES:
            return _LEGACY_UNIT_ALIASES[key].model_copy()
        return UnitSpec(id=key)
    return UnitSpec.coerce(value)


__all__ = ["UnitSpec", "coerce_unit"]
