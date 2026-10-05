"""``type: duckdb`` connection config (``metric_source`` only)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

__all__ = ["DuckDBConnectionConfig"]


class DuckDBConnectionConfig(BaseModel):
    type: Literal["duckdb"] = "duckdb"
    path: Path | str | None = None
    fact_table: str | None = None
    read_only: bool = True
