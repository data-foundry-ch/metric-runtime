"""CLI: run --once / run / store migrate|status / validate / evaluate compat."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from metric_runtime.cli import main

ROOT = Path(__file__).resolve().parents[1]
PYPIZZA = ROOT / "examples" / "pypizza"
DB = PYPIZZA / "data" / "pypizza.duckdb"
PROJECT = PYPIZZA / "metric-runtime.yaml"
CONNECTIONS = PYPIZZA / "connections.yaml"
NOW = "2026-05-15T13:00:00Z"

needs_pypizza = pytest.mark.skipif(not DB.exists(), reason="pypizza.duckdb missing")


def _base(*extra: str, connections: Path = CONNECTIONS, project: Path = PROJECT) -> list[str]:
    return [*extra, "--project-config", str(project), "--connections", str(connections)]


@needs_pypizza
def test_run_once_local_profile_exits_zero(capsys):
    code = main(_base("run", "--once", "--now", NOW, "--log-level", "WARNING"))
    out = capsys.readouterr().out
    assert code == 0
    assert "evaluated=22" in out
    assert "profit_margin @ 2026-05-15T12:30:00+00:00" in out
    assert "next metric due at: 2026-05-15T13:30:00+00:00" in out


@needs_pypizza
def test_run_once_json_report(capsys):
    code = main(_base("run", "--once", "--now", NOW, "--json", "--log-level", "ERROR"))
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["ok"] is True
    assert len(data["outcomes"]) == 22
    assert {o["status"] for o in data["outcomes"]} == {"evaluated"}


@needs_pypizza
def test_run_once_exits_one_when_a_metric_fails(capsys, monkeypatch):
    from metric_runtime.runtime import MetricOutcome, MetricRuntime, RunReport

    def failing(self, now=None):
        report = RunReport(now=now)
        report.outcomes.append(MetricOutcome("orders", now, "failed", error="boom"))
        return report

    monkeypatch.setattr(MetricRuntime, "run_once", failing)
    code = main(_base("run", "--once", "--now", NOW, "--log-level", "ERROR"))
    assert code == 1
    assert "error=boom" in capsys.readouterr().out


def test_now_requires_once(capsys):
    assert main(_base("run", "--now", NOW)) == 1
    assert "--now is only valid with --once" in capsys.readouterr().err


def test_invalid_now_is_a_config_error(capsys):
    assert main(_base("run", "--once", "--now", "yesterday")) == 1
    assert "ISO-8601" in capsys.readouterr().err


def test_unknown_profile_exits_one(capsys):
    assert main(_base("run", "--once", "--profile", "nope")) == 1
    assert "Profile 'nope' not found" in capsys.readouterr().err


@needs_pypizza
def test_run_evaluate_is_deprecated_but_works(capsys):
    code = main(_base("run", "--evaluate", "profit_margin", "--at", "2026-05-15T12:30:00"))
    captured = capsys.readouterr()
    assert code == 0
    assert "deprecated" in captured.err
    assert captured.out.startswith("profit_margin: value=")


@needs_pypizza
def test_evaluate_command(capsys):
    code = main(_base("evaluate", "profit_margin", "--at", "2026-05-15T12:30:00Z"))
    captured = capsys.readouterr()
    assert code == 0
    assert "deprecated" not in captured.err
    assert "profit_margin: value=" in captured.out


def test_store_commands_with_memory_profile(capsys):
    assert main(_base("store", "migrate")) == 0
    assert "nothing to migrate" in capsys.readouterr().out
    assert main(_base("store", "status")) == 0
    assert "in-memory" in capsys.readouterr().out


def test_profile_flag_after_nested_subcommand_is_kept(capsys):
    # --profile given on the outer "store" parser must survive the inner parser.
    assert main(["store", "--profile", "nope", "status"]) == 1
    assert "nope" in capsys.readouterr().err


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_validate_rejects_unknown_schedule_metric(tmp_path, capsys):
    project = _write(
        tmp_path,
        "metric-runtime.yaml",
        PROJECT.read_text(encoding="utf-8").replace(
            "runtime:\n", "runtime:\n  schedules:\n    not_a_metric:\n      enabled: false\n", 1
        ),
    )
    assert "not_a_metric" in project.read_text(encoding="utf-8")
    code = main(_base("validate", project=project))
    assert code == 1
    assert "not_a_metric" in capsys.readouterr().err


def test_validate_rejects_duckdb_runtime_store(tmp_path, capsys):
    conns = _write(
        tmp_path,
        "connections.yaml",
        f"connections:\n  src:\n    type: duckdb\n    path: {DB.as_posix()}\n"
        "profiles:\n  local:\n    metric_source: src\n    runtime_store: src\n",
    )
    assert main(_base("validate", connections=conns)) == 1
    assert "analytical source only" in capsys.readouterr().err


def test_validate_rejects_runtime_tables_in_source_schema(tmp_path, capsys):
    conns = _write(
        tmp_path,
        "connections.yaml",
        "connections:\n"
        "  pg:\n    type: postgres\n    dsn: postgresql://u@db:5432/analytics\n"
        "    schema: public\n    fact_table: facts\n"
        "profiles:\n  local:\n    metric_source: pg\n    runtime_store: pg\n",
    )
    assert main(_base("validate", connections=conns)) == 1
    assert "same database and schema 'public'" in capsys.readouterr().err


def test_validate_postgres_runtime_store_offline_without_env(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("UNSET_DSN_FOR_TEST", raising=False)
    conns = _write(
        tmp_path,
        "connections.yaml",
        f"connections:\n  src:\n    type: duckdb\n    path: {DB.as_posix()}\n"
        "  rt:\n    type: postgres\n    dsn: ${UNSET_DSN_FOR_TEST}\n"
        "profiles:\n  production:\n    metric_source: src\n    runtime_store: rt\n",
    )
    code = main(_base("validate", "--profile", "production", connections=conns))
    out = capsys.readouterr().out
    assert code == 0
    assert "runtime_store=rt" in out


def test_run_with_missing_env_for_used_connection_fails_clearly(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("UNSET_DSN_FOR_TEST", raising=False)
    conns = _write(
        tmp_path,
        "connections.yaml",
        f"connections:\n  src:\n    type: duckdb\n    path: {DB.as_posix()}\n"
        "  rt:\n    type: postgres\n    dsn: ${UNSET_DSN_FOR_TEST}\n"
        "profiles:\n  local:\n    metric_source: src\n"
        "  production:\n    metric_source: src\n    runtime_store: rt\n",
    )
    assert main(_base("run", "--once", "--profile", "production", connections=conns)) == 1
    assert "UNSET_DSN_FOR_TEST" in capsys.readouterr().err


# --- Postgres runtime store (skipped without METRIC_RUNTIME_TEST_POSTGRES_DSN) -------------


def _pg_connections(tmp_path: Path, dsn: str, schema: str) -> Path:
    return _write(
        tmp_path,
        "connections.yaml",
        f"connections:\n  src:\n    type: duckdb\n    path: {DB.as_posix()}\n"
        f"    fact_table: pypizza_halfhourly\n"
        f"  rt:\n    type: postgres\n    dsn: {dsn}\n    schema: {schema}\n"
        "profiles:\n  production:\n    metric_source: src\n    runtime_store: rt\n",
    )


@needs_pypizza
def test_postgres_profile_migrate_run_and_restart(tmp_path, capsys, pg_dsn, pg_schema):
    conns = _pg_connections(tmp_path, pg_dsn, pg_schema)
    args = ["--profile", "production", "--log-level", "ERROR"]

    assert main(_base("run", "--once", "--now", NOW, *args, connections=conns)) == 1
    assert "store migrate" in capsys.readouterr().err

    assert main(_base("store", "status", "--check", *args, connections=conns)) == 1
    assert "pending 001_initial.sql" in capsys.readouterr().out

    assert main(_base("store", "migrate", *args, connections=conns)) == 0
    assert "applied 001_initial.sql" in capsys.readouterr().out
    assert main(_base("store", "migrate", *args, connections=conns)) == 0
    assert "up to date" in capsys.readouterr().out
    assert main(_base("store", "status", "--check", *args, connections=conns)) == 0
    assert "up to date" in capsys.readouterr().out

    assert main(_base("run", "--once", "--now", NOW, "--json", *args, connections=conns)) == 0
    first = json.loads(capsys.readouterr().out)
    assert len(first["outcomes"]) == 22

    # A fresh process (new store instance) sees the committed cursor.
    assert main(_base("run", "--once", "--now", NOW, "--json", *args, connections=conns)) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["outcomes"] == []

    later = "2026-05-15T13:30:00Z"
    assert main(_base("run", "--once", "--now", later, "--json", *args, connections=conns)) == 0
    nxt = json.loads(capsys.readouterr().out)
    assert {o["at"] for o in nxt["outcomes"]} == {"2026-05-15T13:00:00+00:00"}
    detected = [o for o in nxt["outcomes"] if o["transition"].startswith("DETECTED->")]
    assert detected, "state from the previous process must carry over"
