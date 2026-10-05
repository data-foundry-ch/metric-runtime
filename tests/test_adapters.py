"""Adapter registry, capabilities, role wiring and adapter-owned role boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, SecretStr

from metric_runtime.adapters import (
    ENTRY_POINT_GROUP,
    AdapterCapabilities,
    AdapterContext,
    BaseAdapter,
    MetricRuntimeAdapter,
    adapters_supporting,
    get_adapter,
    register_adapter,
    registered_adapters,
    unregister_adapter,
)
from metric_runtime.adapters import registry as registry_module
from metric_runtime.config.factory import build_runtime, validate_profile_wiring
from metric_runtime.config.loader import load_connections_config, redact_connections
from metric_runtime.exceptions import (
    ConfigurationError,
    UnsupportedConnectionTypeError,
    UnsupportedRoleError,
)
from metric_runtime.models import Formula, Metric
from metric_runtime.stores import InMemoryRuntimeStore, ManagedRuntimeStore

ROOT = Path(__file__).resolve().parents[1]


# --- a platform adapter that lives outside core ---------------------------------------------


class FakeWarehouseConfig(BaseModel):
    type: Literal["fakewh"] = "fakewh"
    dataset: str
    runtime_prefix: str = "mr_"
    credentials_json: SecretStr | None = None


class FakeExecutor:
    def __init__(self, dataset: str) -> None:
        self.dataset = dataset

    def metric_value(self, formula, *, at=None, filters=None, start=None, end=None) -> float:
        return 42.0

    def measure_value(self, measure, *, at=None, filters=None, start=None, end=None) -> float:
        return 42.0

    def distinct_groups(self, dimensions, **kwargs) -> list[dict[str, str]]:
        return []


class FakeWarehouseAdapter(BaseAdapter):
    """Source and store in ONE dataset; the boundary is a table prefix."""

    type_name = "fakewh"
    capabilities = AdapterCapabilities(metric_source=True, runtime_store=True)
    config_model = FakeWarehouseConfig

    def __init__(self) -> None:
        self.built: list[tuple[str, str | None]] = []

    def build_executor(self, config: FakeWarehouseConfig, context: AdapterContext) -> Any:
        self.built.append(("metric_source", context.connection_name))
        return FakeExecutor(config.dataset)

    def build_runtime_store(self, config: FakeWarehouseConfig, context: AdapterContext) -> Any:
        self.built.append(("runtime_store", context.connection_name))
        return InMemoryRuntimeStore()

    def role_conflicts(self, *, source_type, source_config, runtime_config, same_connection):
        if source_type == self.type_name and not runtime_config.runtime_prefix:
            return ["runtime_prefix must be set when sharing the source dataset"]
        return []


@pytest.fixture
def fake_adapter():
    adapter = FakeWarehouseAdapter()
    register_adapter("fakewh", adapter)
    try:
        yield adapter
    finally:
        unregister_adapter("fakewh")


def _connections(tmp_path: Path, text: str):
    path = tmp_path / "connections.yaml"
    path.write_text(text, encoding="utf-8")
    cfg, _ = load_connections_config(path, env={})
    return cfg


# --- registry --------------------------------------------------------------------------------


def test_builtin_adapters_and_capabilities():
    assert {"duckdb", "memory", "postgres", "webhook"} <= set(registered_adapters())
    pg = get_adapter("postgres")
    assert isinstance(pg, MetricRuntimeAdapter)
    assert pg.capabilities.metric_source and pg.capabilities.runtime_store
    assert pg.capabilities.durable and pg.capabilities.distributed_claims
    assert pg.capabilities.migrations and not pg.capabilities.notifier
    assert get_adapter("duckdb").capabilities.names() == ["metric_source"]
    assert get_adapter("memory").capabilities.names() == ["runtime_store"]
    assert get_adapter("webhook").capabilities.names() == ["notifier"]
    assert get_adapter("postgres") is pg  # cached instance
    assert adapters_supporting("runtime_store") == ["memory", "postgres"]


def test_core_import_does_not_load_platform_drivers():
    code = (
        "import sys, metric_runtime, metric_runtime.config, metric_runtime.adapters\n"
        "import metric_runtime.execution, metric_runtime.notifications, metric_runtime.stores\n"
        "from metric_runtime.adapters import registered_adapters; registered_adapters()\n"
        "loaded = [m for m in ('psycopg', 'psycopg_pool', 'duckdb') if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


def test_unknown_types_have_actionable_errors():
    with pytest.raises(
        UnsupportedConnectionTypeError, match="pip install metric-runtime-snowflake"
    ):
        get_adapter("snowflake")
    with pytest.raises(UnsupportedConnectionTypeError, match="registered adapters: .*postgres"):
        get_adapter("nope")
    with pytest.raises(UnsupportedConnectionTypeError, match="missing 'type'"):
        get_adapter(None)


def test_register_requires_replace_and_validates_adapters():
    with pytest.raises(ConfigurationError, match="already registered"):
        register_adapter("postgres", FakeWarehouseAdapter)
    register_adapter("broken", object())
    try:
        with pytest.raises(ConfigurationError, match="does not implement MetricRuntimeAdapter"):
            get_adapter("broken")
    finally:
        unregister_adapter("broken")
    register_adapter("postgres", FakeWarehouseAdapter, replace=True)
    try:
        assert isinstance(get_adapter("postgres"), FakeWarehouseAdapter)
    finally:
        unregister_adapter("postgres")
    assert get_adapter("postgres").type_name == "postgres"  # built-in restored


def test_entry_point_adapters_are_discovered(monkeypatch):
    class FakeEntryPoint:
        name = "epwh"
        group = ENTRY_POINT_GROUP

        def load(self):
            return FakeWarehouseAdapter

    seen: list[str] = []

    def fake_entry_points(*, group):
        seen.append(group)
        return [FakeEntryPoint()]

    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    monkeypatch.setattr(registry_module, "_entry_points_loaded", False)
    try:
        adapter = get_adapter("epwh")
        assert isinstance(adapter, FakeWarehouseAdapter)
        assert seen == [ENTRY_POINT_GROUP]
        assert "epwh" in registered_adapters()
    finally:
        unregister_adapter("epwh")


def test_broken_adapter_package_reports_load_failure():
    register_adapter("explodes", "metric_runtime_does_not_exist.adapter:Adapter")
    try:
        with pytest.raises(ConfigurationError, match="'explodes' failed to load"):
            get_adapter("explodes")
        assert "explodes" not in adapters_supporting("metric_source")
    finally:
        unregister_adapter("explodes")


# --- role wiring -----------------------------------------------------------------------------


def test_unsupported_roles_are_rejected_with_alternatives(tmp_path):
    cfg = _connections(
        tmp_path,
        "connections:\n"
        "  local:\n    type: duckdb\n    path: ./x.duckdb\n"
        "  hook:\n    type: webhook\n    url: https://example.test/hook\n"
        "  mem:\n    type: memory\n"
        "profiles:\n"
        "  duck_store:\n    runtime_store: local\n"
        "  hook_source:\n    metric_source: hook\n"
        "  mem_notifier:\n    notifier: mem\n",
    )
    (duck,) = validate_profile_wiring(cfg.profiles["duck_store"], cfg)
    assert "analytical source only" in duck
    assert "adapters supporting runtime_store: memory, postgres" in duck
    (hook,) = validate_profile_wiring(cfg.profiles["hook_source"], cfg)
    assert "'webhook' adapter does not support the metric_source role" in hook
    (mem,) = validate_profile_wiring(cfg.profiles["mem_notifier"], cfg)
    assert "adapters supporting notifier: webhook" in mem


def test_external_adapter_serves_both_roles_from_one_connection(tmp_path, fake_adapter):
    cfg_path = tmp_path / "connections.yaml"
    cfg_path.write_text(
        "connections:\n"
        "  warehouse:\n    type: fakewh\n    dataset: analytics\n    credentials_json: hush\n"
        "profiles:\n  production:\n    metric_source: warehouse\n    runtime_store: warehouse\n",
        encoding="utf-8",
    )
    cfg, _ = load_connections_config(cfg_path, env={})
    # Same dataset for both roles is fine: this adapter's boundary is a table prefix.
    assert validate_profile_wiring(cfg.profiles["production"], cfg) == []

    project = tmp_path / "metric-runtime.yaml"
    project.write_text("runtime:\n  default_profile: production\n", encoding="utf-8")
    engine = build_runtime(
        None,
        project_config=project,
        connections_config=cfg_path,
        catalog=[Metric(id="orders", name="Orders", formula=Formula.sum("orders"))],
    )
    assert isinstance(engine.executor, FakeExecutor)
    assert isinstance(engine.runtime_store, InMemoryRuntimeStore)
    # One connection, two role-specific resources.
    assert sorted(fake_adapter.built) == [
        ("metric_source", "warehouse"),
        ("runtime_store", "warehouse"),
    ]
    from datetime import UTC, datetime

    assert engine.metric_value("orders", datetime(2026, 5, 15, tzinfo=UTC)) == 42.0


def test_role_boundary_is_owned_by_the_store_adapter(tmp_path, fake_adapter):
    cfg = _connections(
        tmp_path,
        "connections:\n"
        "  warehouse:\n    type: fakewh\n    dataset: analytics\n    runtime_prefix: ''\n"
        "profiles:\n  production:\n    metric_source: warehouse\n    runtime_store: warehouse\n",
    )
    (error,) = validate_profile_wiring(cfg.profiles["production"], cfg)
    assert error == (
        "runtime_store 'warehouse' / metric_source 'warehouse': "
        "runtime_prefix must be set when sharing the source dataset"
    )


def test_mixed_platforms_resolve_each_role_independently(tmp_path, fake_adapter):
    cfg = _connections(
        tmp_path,
        "connections:\n"
        "  local:\n    type: duckdb\n    path: ./x.duckdb\n"
        "  warehouse:\n    type: fakewh\n    dataset: analytics\n"
        "  ops:\n    type: postgres\n    dsn: postgresql://u@ops:5432/ops\n"
        "    runtime_schema: public\n"
        "profiles:\n"
        "  duck_to_pg:\n    metric_source: local\n    runtime_store: ops\n"
        "  fake_to_pg:\n    metric_source: warehouse\n    runtime_store: ops\n"
        "  pg_to_fake:\n    metric_source: ops\n    runtime_store: warehouse\n",
    )
    # Different platforms never collide, even with the same schema/dataset names.
    for name in ("duck_to_pg", "fake_to_pg", "pg_to_fake"):
        assert validate_profile_wiring(cfg.profiles[name], cfg) == [], name


# --- Postgres boundary (offline) ------------------------------------------------------------


def _pg_profile(tmp_path, source: str, store: str | None = None):
    store_block = f"  store:\n    type: postgres\n{store}" if store else ""
    cfg = _connections(
        tmp_path,
        "connections:\n"
        f"  warehouse:\n    type: postgres\n{source}"
        f"{store_block}"
        "profiles:\n  p:\n    metric_source: warehouse\n"
        f"    runtime_store: {'store' if store else 'warehouse'}\n",
    )
    return validate_profile_wiring(cfg.profiles["p"], cfg)


def test_postgres_one_connection_two_schemas_is_valid(tmp_path):
    source = (
        "    dsn: postgresql://u@db:5432/app\n"
        "    source_schema: analytics\n    runtime_schema: metric_runtime\n"
    )
    assert _pg_profile(tmp_path, source) == []


def test_postgres_rejects_runtime_schema_equal_to_source_schema(tmp_path):
    source = "    dsn: postgresql://u@db:5432/app\n    source_schema: analytics\n"
    source += "    runtime_schema: analytics\n"
    (error,) = _pg_profile(tmp_path, source)
    assert "effective source schema 'analytics'" in error


def test_postgres_qualified_fact_table_wins_over_source_schema(tmp_path):
    source = (
        "    dsn: postgresql://u@db:5432/app\n    source_schema: public\n"
        "    fact_table: metric_runtime.orders\n    runtime_schema: metric_runtime\n"
    )
    (error,) = _pg_profile(tmp_path, source)
    assert "effective source schema 'metric_runtime'" in error


def test_postgres_separate_connections_same_database_are_compared(tmp_path):
    source = "    dsn: postgresql://reader@db:5432/app\n    fact_table: analytics.facts\n"
    same_db = "    host: DB\n    database: app\n    runtime_schema: analytics\n"
    (error,) = _pg_profile(tmp_path, source, same_db)
    assert "runtime_schema 'analytics'" in error
    other_db = "    host: other\n    database: app\n    runtime_schema: analytics\n"
    assert _pg_profile(tmp_path, source, other_db) == []


# --- managed stores, secrets, back-compat ----------------------------------------------------


def test_managed_runtime_store_is_runtime_checkable():
    assert not isinstance(InMemoryRuntimeStore(), ManagedRuntimeStore)
    pytest.importorskip("psycopg_pool")
    from metric_runtime.adapters.postgres.store import PostgresRuntimeStore

    store = PostgresRuntimeStore(schema="mr_test", pool=object())
    assert isinstance(store, ManagedRuntimeStore)
    assert store.namespace == "schema mr_test"


def test_adapter_declared_secrets_are_redacted(fake_adapter):
    shown = redact_connections(
        {
            "warehouse": {"type": "fakewh", "dataset": "analytics", "credentials_json": "{k}"},
            "pg": {"type": "postgres", "dsn": "postgresql://u:leak@h/db", "password": "pw"},
            "unknown": {"type": "nope", "api_key": "k"},
        }
    )
    assert shown["warehouse"]["credentials_json"] == "***REDACTED***"
    assert shown["warehouse"]["dataset"] == "analytics"
    assert "leak" not in str(shown) and "pw" not in str(shown["pg"])
    assert shown["unknown"]["api_key"] == "***REDACTED***"


def test_unsupported_role_error_type(tmp_path):
    from metric_runtime.config.factory import build_runtime_store

    cfg = _connections(
        tmp_path,
        "connections:\n  local:\n    type: duckdb\n    path: ./x.duckdb\n"
        "profiles:\n  p:\n    runtime_store: local\n",
    )
    with pytest.raises(UnsupportedRoleError, match="analytical source only"):
        build_runtime_store(cfg.profiles["p"], cfg)


def test_old_import_paths_still_work():
    from metric_runtime.config.models import (
        DuckDBConnectionConfig,
        MemoryStateStoreConfig,
        PostgresConnectionConfig,
        WebhookConnectionConfig,
    )
    from metric_runtime.execution import DuckDBExecutor, PostgresExecutor
    from metric_runtime.execution.duckdb import quote_identifier
    from metric_runtime.notifications import WebhookNotifier
    from metric_runtime.notifications.webhook import sign_body
    from metric_runtime.stores.migrations import load_migrations, validate_schema_name

    assert DuckDBConnectionConfig is get_adapter("duckdb").config_model
    assert MemoryStateStoreConfig is get_adapter("memory").config_model
    assert PostgresConnectionConfig is get_adapter("postgres").config_model
    assert WebhookConnectionConfig is get_adapter("webhook").config_model
    assert DuckDBExecutor.__module__ == "metric_runtime.adapters.duckdb.executor"
    assert PostgresExecutor.__module__ == "metric_runtime.adapters.postgres.executor"
    assert WebhookNotifier.__module__ == "metric_runtime.adapters.webhook.notifier"
    assert quote_identifier("a") == '"a"' and sign_body("s", b"x").startswith("sha256=")
    assert load_migrations()[0].filename == "001_initial.sql"
    assert validate_schema_name("metric_runtime") == "metric_runtime"
    pytest.importorskip("psycopg_pool")
    from metric_runtime.stores.postgres import PostgresRuntimeStore

    assert PostgresRuntimeStore.__module__ == "metric_runtime.adapters.postgres.store"
