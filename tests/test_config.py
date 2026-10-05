"""Configuration loading / factory / secret redaction tests."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from metric_runtime.config.environment import resolve_environment_variables
from metric_runtime.config.factory import build_runtime, show_resolved_config
from metric_runtime.config.loader import load_connections_config, load_project_config
from metric_runtime.config.models import MetricRuntimeProjectConfig, SecretFieldDemo
from metric_runtime.exceptions import (
    ConfigurationError,
    MissingEnvironmentVariableError,
    UnknownConnectionError,
    UnsupportedConnectionTypeError,
)
from metric_runtime.execution import DuckDBExecutor
from metric_runtime.stores import InMemoryStateStore

ROOT = Path(__file__).resolve().parents[1]
PYPIZZA = ROOT / "examples" / "pypizza"


def test_valid_project_yaml_parses():
    cfg, path = load_project_config(PYPIZZA / "metric-runtime.yaml")
    assert path is not None
    assert cfg.project.name == "pypizza"
    assert cfg.state.detections_before_open == 2


def test_invalid_project_config_readable(tmp_path: Path):
    bad = tmp_path / "metric-runtime.yaml"
    bad.write_text("state:\n  detections_before_open: not-a-number\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_project_config(bad)
    assert "Invalid" in str(exc.value) or "detections" in str(exc.value).lower()


def test_valid_connections_yaml_parses():
    cfg, path = load_connections_config(PYPIZZA / "connections.yaml", resolve_env=False)
    assert path is not None
    assert "pypizza" in cfg.connections
    assert "local" in cfg.profiles


def test_unknown_connection_type_fails(tmp_path: Path):
    path = tmp_path / "connections.yaml"
    path.write_text(
        "connections:\n  x:\n    type: mysterio\nprofiles:\n  local:\n    metric_source: x\n",
        encoding="utf-8",
    )
    cfg, _ = load_connections_config(path, resolve_env=False)
    with pytest.raises(
        (ConfigurationError, UnsupportedConnectionTypeError, ValidationError, ValueError)
    ):
        cfg.get_connection("x")


def test_profile_missing_connection_fails(tmp_path: Path):
    path = tmp_path / "connections.yaml"
    path.write_text(
        "connections: {}\nprofiles:\n  local:\n    metric_source: missing\n",
        encoding="utf-8",
    )
    cfg, _ = load_connections_config(path, resolve_env=False)
    with pytest.raises(UnknownConnectionError):
        cfg.get_connection("missing")


def test_missing_env_var_fails_clearly():
    with pytest.raises(MissingEnvironmentVariableError) as exc:
        resolve_environment_variables(
            {"password": "${DOES_NOT_EXIST_METRIC_RUNTIME}"},
            context='connection "warehouse"',
            env={},
        )
    msg = str(exc.value)
    assert "DOES_NOT_EXIST_METRIC_RUNTIME" in msg
    assert "warehouse" in msg


def test_secret_repr_redacted():
    demo = SecretFieldDemo(password=SecretStr("super-secret-value"))
    text = repr(demo)
    assert "super-secret-value" not in text
    assert "**********" in text or "SecretStr" in text


def test_secret_redacted_in_diagnostics():
    from metric_runtime.config.loader import redact_secrets

    data = redact_secrets({"user": "alice", "password": "hunter2", "nested": {"token": "abc"}})
    assert data["password"] == "***REDACTED***"
    assert data["nested"]["token"] == "***REDACTED***"
    assert data["user"] == "alice"


def test_local_profile_builds_duckdb_and_memory():
    db = PYPIZZA / "data" / "pypizza.duckdb"
    if not db.exists():
        pytest.skip("pypizza.duckdb missing — run generate_data.py")
    engine = build_runtime(
        "local",
        project_config=PYPIZZA / "metric-runtime.yaml",
        connections_config=PYPIZZA / "connections.yaml",
    )
    assert isinstance(engine.executor, DuckDBExecutor)
    assert isinstance(engine.state_store, InMemoryStateStore)
    assert engine.state_policy.min_impact_eur == 40.0
    assert engine.state_policy.persistence == 2
    assert len(engine.catalog) > 5


def test_explicit_paths_override_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "metric-runtime.yaml").write_text("project:\n  name: wrong\n", encoding="utf-8")
    cfg, path = load_project_config(PYPIZZA / "metric-runtime.yaml")
    assert cfg.project.name == "pypizza"
    assert path == PYPIZZA / "metric-runtime.yaml"


def test_config_show_redacts(tmp_path: Path):
    path = tmp_path / "connections.yaml"
    path.write_text(
        "connections:\n  x:\n    type: duckdb\n    path: ./x.duckdb\n"
        "    password: should-not-leak\n"
        "profiles:\n  local:\n    metric_source: x\n    state_store:\n      type: memory\n",
        encoding="utf-8",
    )
    project = tmp_path / "metric-runtime.yaml"
    project.write_text("project:\n  name: t\n", encoding="utf-8")
    shown = show_resolved_config("local", project_config=project, connections_config=path)
    blob = str(shown)
    assert "should-not-leak" not in blob
    assert "***REDACTED***" in blob


def test_project_config_model_defaults():
    cfg = MetricRuntimeProjectConfig()
    assert cfg.runtime.default_profile == "local"
    assert cfg.runtime.evaluation_lag == "0m"
    assert cfg.runtime.max_catchup_windows == 1
    assert cfg.runtime.idle_interval == "5m"
    assert cfg.runtime.schedules == {}
    assert cfg.runtime.notifications.max_attempts == 10


def test_runtime_schedule_config_parses():
    from metric_runtime.runtime import RuntimeSchedule

    cfg = MetricRuntimeProjectConfig.model_validate(
        {
            "runtime": {
                "evaluation_interval": "30m",
                "evaluation_lag": "5m",
                "max_catchup_windows": 4,
                "idle_interval": "2m",
                "schedules": {
                    "orders": {"evaluation_interval": "1h"},
                    "legacy": {"enabled": False},
                },
                "notifications": {"max_attempts": 3, "backoff_initial": "10s", "lease": "1m"},
            }
        }
    )
    schedule = RuntimeSchedule.from_config(cfg.runtime)
    assert schedule.default_interval == timedelta(minutes=30)
    assert schedule.lag == timedelta(minutes=5)
    assert schedule.max_catchup_windows == 4
    assert schedule.idle_interval == timedelta(minutes=2)
    assert schedule.interval_for("orders") == timedelta(hours=1)
    assert schedule.interval_for("revenue") == timedelta(minutes=30)
    assert not schedule.is_enabled("legacy")
    assert schedule.is_enabled("orders")


@pytest.mark.parametrize(
    "runtime",
    [
        {"evaluation_interval": "0m"},
        {"idle_interval": "0s"},
        {"max_catchup_windows": 0},
        {"evaluation_lag": "soon"},
        {"notifications": {"lease": "0m"}},
        {"notifications": {"max_attempts": 0}},
        {"schedules": {"orders": {"evaluation_interval": "0h"}}},
    ],
)
def test_invalid_runtime_config_rejected(runtime):
    with pytest.raises(ValidationError):
        MetricRuntimeProjectConfig.model_validate({"runtime": runtime})


def test_state_store_is_alias_for_runtime_store():
    from metric_runtime.config.models import ProfileConfig

    legacy = ProfileConfig.model_validate({"state_store": "db"})
    assert legacy.runtime_store == "db"
    both = ProfileConfig.model_validate({"state_store": "db", "runtime_store": "db"})
    assert both.runtime_store == "db"
    with pytest.raises(ValidationError, match="runtime_store"):
        ProfileConfig.model_validate({"state_store": "a", "runtime_store": "b"})


def test_postgres_connection_config():
    from metric_runtime.config.models import PostgresConnectionConfig

    cfg = PostgresConnectionConfig.model_validate(
        {
            "type": "postgres",
            "host": "db.internal",
            "database": "ops",
            "user": "runtime",
            "password": "s3cret",
            "sslmode": "require",
            "schema": "mr_prod",
        }
    )
    assert cfg.schema_name == "mr_prod"
    assert cfg.conninfo() == ""
    kwargs = cfg.connect_kwargs()
    assert kwargs["host"] == "db.internal" and kwargs["dbname"] == "ops"
    assert kwargs["password"] == "s3cret" and kwargs["sslmode"] == "require"
    assert "s3cret" not in repr(cfg)

    dsn = PostgresConnectionConfig.model_validate({"dsn": "postgresql://u:pw@h/db"})
    assert dsn.conninfo() == "postgresql://u:pw@h/db"
    assert "pw@" not in repr(dsn)

    with pytest.raises(ValidationError, match="dsn"):
        PostgresConnectionConfig.model_validate({"host": "h"})
    with pytest.raises(ValidationError):
        PostgresConnectionConfig.model_validate({"dsn": "x", "schema": "bad-name;drop"})


def test_postgres_source_and_runtime_schemas():
    from metric_runtime.config.models import PostgresConnectionConfig

    cfg = PostgresConnectionConfig.model_validate(
        {"dsn": "postgresql://h/db", "source_schema": "analytics", "fact_table": "facts"}
    )
    assert cfg.runtime_schema == cfg.schema_name == "metric_runtime"
    assert cfg.effective_fact_table == "analytics.facts"
    assert cfg.effective_source_schema == "analytics"
    qualified = cfg.model_copy(update={"fact_table": "marts.orders"})
    assert qualified.effective_fact_table == "marts.orders"
    assert qualified.effective_source_schema == "marts"
    bare = PostgresConnectionConfig.model_validate({"dsn": "x"})
    assert bare.effective_source_schema == "public" and bare.effective_fact_table is None

    legacy = PostgresConnectionConfig.model_validate({"dsn": "x", "schema": "mr"})
    same = PostgresConnectionConfig.model_validate(
        {"dsn": "x", "schema": "mr", "runtime_schema": "mr"}
    )
    assert legacy.runtime_schema == same.runtime_schema == "mr"
    with pytest.raises(ValidationError, match="runtime_schema only"):
        PostgresConnectionConfig.model_validate({"dsn": "x", "schema": "a", "runtime_schema": "b"})
    with pytest.raises(ValidationError, match="source_schema"):
        PostgresConnectionConfig.model_validate({"dsn": "x", "source_schema": "a b"})

    dumped = cfg.model_dump(mode="json")
    assert dumped["dsn"] == "**********"  # SecretStr never serializes its value
    round_trip = PostgresConnectionConfig.model_validate({**dumped, "dsn": "postgresql://h/db"})
    assert round_trip == cfg


def test_build_runtime_store_roles(tmp_path: Path):
    from metric_runtime.config.factory import build_runtime_store, validate_profile_wiring

    path = tmp_path / "connections.yaml"
    path.write_text(
        "connections:\n"
        "  src:\n    type: duckdb\n    path: ./x.duckdb\n"
        "  mem:\n    type: memory\n"
        "  pg:\n    type: postgres\n    dsn: ${PG_DSN_NOT_SET_FOR_TEST}\n"
        "profiles:\n"
        "  ok:\n    metric_source: src\n    runtime_store: mem\n"
        "  inline:\n    metric_source: src\n"
        "  wrong_store:\n    runtime_store: src\n"
        "  pg_source:\n    metric_source: pg\n"
        "  missing:\n    runtime_store: nope\n",
        encoding="utf-8",
    )
    cfg, _ = load_connections_config(path, env={})
    assert isinstance(build_runtime_store(cfg.profiles["ok"], cfg), InMemoryStateStore)
    assert isinstance(build_runtime_store(cfg.profiles["inline"], cfg), InMemoryStateStore)
    assert validate_profile_wiring(cfg.profiles["ok"], cfg) == []
    assert "analytical source only" in validate_profile_wiring(cfg.profiles["wrong_store"], cfg)[0]
    # Postgres is a valid source; this one fails only because its env var is unset.
    pg_errors = validate_profile_wiring(cfg.profiles["pg_source"], cfg)
    assert "PG_DSN_NOT_SET_FOR_TEST" in pg_errors[0]
    assert validate_profile_wiring(cfg.profiles["pg_source"], cfg, check_fields=False) == []
    assert "unknown connection" in validate_profile_wiring(cfg.profiles["missing"], cfg)[0]
    # The unresolved env var only matters when a profile uses that connection.
    with pytest.raises(MissingEnvironmentVariableError, match="PG_DSN_NOT_SET_FOR_TEST"):
        cfg.get_connection("pg")


def test_config_show_redacts_dsn(tmp_path: Path):
    path = tmp_path / "connections.yaml"
    path.write_text(
        "connections:\n  rt:\n    type: postgres\n    dsn: postgresql://u:leaky@h/db\n"
        "profiles:\n  production:\n    runtime_store: rt\n",
        encoding="utf-8",
    )
    project = tmp_path / "metric-runtime.yaml"
    project.write_text("runtime:\n  default_profile: production\n", encoding="utf-8")
    shown = show_resolved_config(None, project_config=project, connections_config=path)
    assert shown["profile"] == "production"
    assert "leaky" not in str(shown)


def test_build_metric_runtime_from_profile():
    db = PYPIZZA / "data" / "pypizza.duckdb"
    if not db.exists():
        pytest.skip("pypizza.duckdb missing — run generate_data.py")
    from metric_runtime.config.factory import build_metric_runtime

    runtime = build_metric_runtime(
        None,
        project_config=PYPIZZA / "metric-runtime.yaml",
        connections_config=PYPIZZA / "connections.yaml",
    )
    try:
        assert runtime.schedule.default_interval == timedelta(minutes=30)
        assert len(runtime.scheduled_metrics()) == len(runtime.engine.catalog)
        assert runtime.engine.notification_policy.max_attempts == 10
    finally:
        runtime.close()
