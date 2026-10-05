"""Postgres — the reference full adapter (``metric_source`` + durable ``runtime_store``).

One connection can serve both roles::

    warehouse config
      ├── PostgresExecutor       own connection, read-only sessions, source_schema
      └── PostgresRuntimeStore   own writable pool, runtime_schema

The boundary between them is a schema: runtime tables must never live in the
schema business data is read from.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from metric_runtime.adapters.base import AdapterCapabilities, AdapterContext, BaseAdapter
from metric_runtime.adapters.postgres.config import PostgresConnectionConfig
from metric_runtime.config.duration import parse_duration
from metric_runtime.exceptions import ConfigurationError

__all__ = ["PostgresAdapter"]


class PostgresAdapter(BaseAdapter):
    type_name = "postgres"
    capabilities = AdapterCapabilities(
        metric_source=True,
        runtime_store=True,
        durable=True,
        distributed_claims=True,
        migrations=True,
    )
    config_model = PostgresConnectionConfig
    install_hint = 'pip install "metric-runtime[postgres]"'

    def build_executor(self, config: PostgresConnectionConfig, context: AdapterContext) -> Any:
        from metric_runtime.adapters.postgres.executor import PostgresExecutor

        fact_table = config.effective_fact_table or context.project.runtime.fact_table
        timeout = parse_duration(config.statement_timeout, field="statement_timeout")
        try:
            return PostgresExecutor(
                config.conninfo(),
                fact_table=fact_table,
                timestamp_column=config.timestamp_column,
                statement_timeout_ms=int(timeout.total_seconds() * 1000),
                search_path=config.source_schema,
                connect_kwargs=config.connect_kwargs(),
            )
        except ValueError as exc:
            raise ConfigurationError(f"Invalid postgres metric_source: {exc}") from exc

    def build_runtime_store(self, config: PostgresConnectionConfig, context: AdapterContext) -> Any:
        from metric_runtime.adapters.postgres.store import PostgresRuntimeStore

        return PostgresRuntimeStore(
            config.conninfo(),
            schema=config.runtime_schema,
            min_size=config.pool_min_size,
            max_size=config.pool_max_size,
            claim_ttl=parse_duration(config.claim_ttl, field="claim_ttl").total_seconds(),
            connect_kwargs=config.connect_kwargs(),
        )

    def role_conflicts(
        self,
        *,
        source_type: str,
        source_config: BaseModel,
        runtime_config: PostgresConnectionConfig,
        same_connection: bool,
    ) -> list[str]:
        if source_type != self.type_name or not isinstance(source_config, PostgresConnectionConfig):
            return []
        same_db = same_connection
        if not same_db:
            source_id = source_config.database_identity()
            same_db = source_id is not None and source_id == runtime_config.database_identity()
        source_schema = source_config.effective_source_schema
        if same_db and runtime_config.runtime_schema == source_schema:
            return [
                f"runtime_schema {runtime_config.runtime_schema!r} is also the schema business "
                f"data is read from (effective source schema {source_schema!r}) on the same "
                "database; give the runtime store its own schema so runtime tables never mix "
                "with source data"
            ]
        return []
