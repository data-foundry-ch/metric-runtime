"""Shared fixtures: runtime stores parametrized over memory and Postgres.

Postgres cases skip unless ``METRIC_RUNTIME_TEST_POSTGRES_DSN`` is set. Each
test gets a unique schema that is dropped afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import pytest

PG_DSN_ENV = "METRIC_RUNTIME_TEST_POSTGRES_DSN"


@pytest.fixture
def pg_dsn() -> str:
    dsn = os.environ.get(PG_DSN_ENV)
    if not dsn:
        pytest.skip(f"{PG_DSN_ENV} not set")
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")
    return dsn


@pytest.fixture
def pg_schema(pg_dsn: str) -> Iterator[str]:
    import psycopg

    schema = f"mrt_{uuid.uuid4().hex[:12]}"
    yield schema
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


StoreFactory = Callable[..., object]


@pytest.fixture(params=["memory", "postgres"])
def store_kind(request) -> str:
    return request.param


@pytest.fixture
def store_factory(store_kind: str, request) -> Iterator[StoreFactory]:
    """Callable creating runtime stores.

    For Postgres every call returns a NEW store instance over the SAME schema
    (simulating a restart or a second worker process). For memory every call
    returns the same instance (process-local by design).
    """
    created: list[object] = []
    if store_kind == "memory":
        from metric_runtime.stores.memory import InMemoryRuntimeStore

        shared: dict[str, object] = {}

        def make_memory(**kwargs) -> object:
            if kwargs or "store" not in shared:
                store = InMemoryRuntimeStore(**kwargs)
                if not kwargs:
                    shared["store"] = store
                return store
            return shared["store"]

        yield make_memory
        return

    dsn = request.getfixturevalue("pg_dsn")
    schema = request.getfixturevalue("pg_schema")
    from metric_runtime.stores.postgres import PostgresRuntimeStore

    def make_pg(**kwargs) -> object:
        store = PostgresRuntimeStore(dsn, schema=schema, min_size=1, max_size=8, **kwargs)
        store.migrate()
        created.append(store)
        return store

    yield make_pg
    for store in created:
        store.close()  # type: ignore[attr-defined]
