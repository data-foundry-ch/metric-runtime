"""Postgres adapter: read-only analytical source and durable runtime store.

Requires ``pip install "metric-runtime[postgres]"`` (psycopg 3 + psycopg-pool);
the driver is only imported when a connection is actually opened.
"""

from metric_runtime.adapters.postgres.adapter import PostgresAdapter
from metric_runtime.adapters.postgres.config import PostgresConnectionConfig

__all__ = ["PostgresAdapter", "PostgresConnectionConfig"]
