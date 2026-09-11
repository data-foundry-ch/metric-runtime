"""Stable identity helpers for scopes and evaluation windows."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class NaiveDatetimeError(ValueError):
    """Raised when a timezone-naive datetime is passed to the core runtime."""


def require_aware(value: datetime, *, field: str = "datetime") -> datetime:
    """Require timezone-aware datetimes; normalize to UTC."""
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime, got {type(value)!r}")
    if value.tzinfo is None:
        raise NaiveDatetimeError(
            f"{field} must be timezone-aware (got naive {value!r}). "
            "Pass an explicit tzinfo such as datetime.UTC."
        )
    return value.astimezone(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Normalize timezone-aware datetimes to UTC (rejects naive)."""
    return require_aware(value)


def parse_datetime(value: Any, *, field: str = "datetime") -> datetime:
    """Parse ISO strings / aware datetimes into timezone-aware UTC.

    Naive datetime objects are rejected. ISO strings without an offset are
    rejected as well — callers must include ``Z`` or an explicit offset.
    """
    if isinstance(value, datetime):
        return require_aware(value, field=field)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} must not be empty")
        if " → " in text:
            text = text.split(" → ", 1)[1].strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise NaiveDatetimeError(
                f"{field} ISO string must include a timezone offset "
                f"(got {value!r}). Example: '2026-05-15T12:00:00+00:00'."
            )
        return parsed.astimezone(UTC)
    raise TypeError(f"Expected datetime or ISO string for {field}, got {type(value)!r}")


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
        return parse_datetime(value, field="EvaluationKey timestamp")

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
            return require_aware(self.at).isoformat()
        assert self.start is not None and self.end is not None
        return f"{require_aware(self.start).isoformat()}/{require_aware(self.end).isoformat()}"

    @property
    def eval_at(self) -> datetime:
        if self.at is not None:
            return require_aware(self.at)
        assert self.end is not None
        return require_aware(self.end)

    @property
    def identity(self) -> str:
        return f"{self.metric}|{self.scope_key}|{self.window_id}"

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
