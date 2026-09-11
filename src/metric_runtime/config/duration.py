"""Strict duration parsing for project configuration."""

from __future__ import annotations

import re
from datetime import timedelta

_DURATION_RE = re.compile(r"^(\d+)(s|m|h|d)$")


def parse_duration(value: str, *, field: str = "duration") -> timedelta:
    """Parse a documented duration string into a timedelta.

    Supported syntax: ``Ns``, ``Nm``, ``Nh``, ``Nd`` (non-negative integers).
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid {field}: expected a non-empty duration string like '30m'")
    text = value.strip()
    match = _DURATION_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"Invalid {field} {value!r}. Expected one of: '30s', '15m', '2h', '1d'.")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(days=amount)


def parse_duration_minutes(value: str, *, field: str = "duration") -> int:
    """Parse a duration and return whole minutes (ceil for sub-minute values)."""
    delta = parse_duration(value, field=field)
    seconds = int(delta.total_seconds())
    if seconds % 60 == 0:
        return seconds // 60
    return (seconds + 59) // 60
