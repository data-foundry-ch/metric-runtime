"""Configuration loading / factory / secret redaction tests."""

from __future__ import annotations

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
