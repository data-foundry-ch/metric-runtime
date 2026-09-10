"""Stable identity helpers for scopes and evaluation windows."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def ensure_utc(value: datetime) -> datetime:
    """Normalize datetimes to timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_datetime(value: Any) -> datetime:
    """Parse ISO strings / datetimes into timezone-aware UTC."""
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        text = value.strip()
        if " → " in text:
            # Legacy window label: take the end bound.
            text = text.split(" → ", 1)[1].strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return ensure_utc(datetime.fromisoformat(text))
    raise TypeError(f"Expected datetime or ISO string, got {type(value)!r}")


def canonical_scope_json(scope: dict[str, str] | None = None) -> str:
    """Deterministic JSON for scope diagnostics and hashing."""
    payload = {str(k): str(v) for k, v in sorted((scope or {}).items())}
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def canonical_scope_key(scope: dict[str, str] | None = None) -> str:
    """Collision-resistant scope identity (SHA-256 of canonical JSON)."""
    digest = hashlib.sha256(canonical_scope_json(scope).encode("utf-8")).hexdigest()
    return digest


class EvaluationKey(BaseModel):
    """Identity of one runtime evaluation before any warehouse work runs."""

    metric: str
    scope: dict[str, str] = Field(default_factory=dict)
    scope_key: str = ""
    scope_json: str = ""
    at: datetime | None = None
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("at", "start", "end", mode="before")
    @classmethod
    def _coerce_dt(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        return parse_datetime(value)

    @model_validator(mode="after")
    def _defaults(self) -> EvaluationKey:
        scope = dict(self.scope)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "scope_json", canonical_scope_json(scope))
        object.__setattr__(self, "scope_key", canonical_scope_key(scope))
        if self.at is None and (self.start is None or self.end is None):
            raise ValueError("EvaluationKey requires at= or start=/end=")
        if self.at is not None and (self.start is not None or self.end is not None):
            raise ValueError("EvaluationKey accepts either at= or start=/end=, not both")
        return self

    @property
    def window_id(self) -> str:
        if self.at is not None:
            return ensure_utc(self.at).isoformat()
        assert self.start is not None and self.end is not None
        return f"{ensure_utc(self.start).isoformat()}/{ensure_utc(self.end).isoformat()}"

    @property
    def eval_at(self) -> datetime:
        if self.at is not None:
            return ensure_utc(self.at)
        assert self.end is not None
        return ensure_utc(self.end)

    @classmethod
    def build(
        cls,
        metric: str,
        *,
        scope: dict[str, str] | None = None,
        at: datetime | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> EvaluationKey:
        return cls(metric=metric, scope=dict(scope or {}), at=at, start=start, end=end)
