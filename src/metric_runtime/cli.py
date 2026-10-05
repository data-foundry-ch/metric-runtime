"""metric-runtime CLI.

Python API first. Configuration files for deployment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from metric_runtime.stores.base import RuntimeStore


def _load_project_catalog(args: argparse.Namespace):
    from metric_runtime.config.factory import load_catalog_entrypoint
    from metric_runtime.config.loader import load_project_config
    from metric_runtime.exceptions import ConfigurationError, MetricRuntimeError

    project, project_path = load_project_config(getattr(args, "project_config", None))
    entrypoint = project.catalog.entrypoint or project.catalog.module
    if not entrypoint:
        raise ConfigurationError(
            "No catalog configured. Set catalog.entrypoint in metric-runtime.yaml "
            "(e.g. metrics.catalog:catalog)."
        )
    base = project_path.parent if project_path is not None else Path.cwd()
    try:
        return load_catalog_entrypoint(entrypoint, base_dir=base), entrypoint
    except MetricRuntimeError:
        raise


def _cmd_config_show(args: argparse.Namespace) -> int:
    from metric_runtime.config.factory import show_resolved_config
    from metric_runtime.exceptions import MetricRuntimeError

    try:
        data = show_resolved_config(
            args.profile,
            project_config=args.project_config,
            connections_config=args.connections,
        )
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, default=str))
    return 0


def _describe_role(connections, ref, default: str) -> str:
    """``name (type: capabilities)`` for a connection, or the inline/default description."""
    from metric_runtime.adapters import get_adapter

    if ref is None:
        return default
    if not isinstance(ref, str):
        return f"{ref.type} (inline)"
    ctype = connections.connection_type(ref)
    capabilities = ", ".join(get_adapter(ctype).capabilities.names())
    return f"{ref} ({ctype}: {capabilities})"


def _cmd_validate(args: argparse.Namespace) -> int:
    from metric_runtime.config.environment import find_env_placeholders
    from metric_runtime.config.factory import (
        load_catalog_entrypoint,
        resolve_profile,
        validate_profile_wiring,
    )
    from metric_runtime.config.loader import (
        DEFAULT_CONNECTIONS_FILENAMES,
        _read_yaml,
        discover_config_path,
        load_connections_config,
        load_project_config,
    )
    from metric_runtime.exceptions import MetricRuntimeError

    errors: list[str] = []
    try:
        project, project_path = load_project_config(args.project_config)
        print(f"project config: {project_path or '(defaults)'}")
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Connections schema is validated offline; env vars are only reported, not required
    # unless the user also wants connection resolution. Default validate stays offline.
    connections = None
    connections_path = None
    try:
        connections, connections_path = load_connections_config(args.connections, resolve_env=False)
        print(f"connections config: {connections_path or '(none)'}")
    except MetricRuntimeError as exc:
        # Offline validate can proceed without connections when only catalog is needed.
        print(f"connections config: skipped ({exc})")

    profile_name = args.profile or project.runtime.default_profile
    if connections is not None and connections.profiles:
        try:
            profile_cfg = resolve_profile(connections, profile_name, project)
            print(f"profile: {profile_name}")
            wiring_errors = validate_profile_wiring(profile_cfg, connections, check_fields=False)
            errors.extend(f"profile {profile_name!r}: {err}" for err in wiring_errors)
            if not wiring_errors:
                print("roles:")
                for role, ref, default in (
                    ("metric_source", profile_cfg.metric_source, "-"),
                    ("runtime_store", profile_cfg.runtime_store, "memory (default)"),
                    ("notifier", profile_cfg.notifier, "logging (default)"),
                ):
                    print(f"  {role}={_describe_role(connections, ref, default)}")
        except MetricRuntimeError as exc:
            errors.append(str(exc))
    else:
        print(f"profile: {profile_name} (no connections profiles; catalog-only)")

    path = discover_config_path(args.connections, defaults=DEFAULT_CONNECTIONS_FILENAMES)
    if path is not None and not args.offline_catalog_only:
        raw = _read_yaml(path)
        missing = []
        import os

        for name in sorted(find_env_placeholders(raw)):
            if not os.environ.get(name):
                missing.append(name)
        if missing:
            # Report but do not fail offline semantic validation by default.
            print(
                "note: missing env vars for connections (not required for catalog validate): "
                + ", ".join(missing)
            )

    catalog = None
    entrypoint = project.catalog.entrypoint or project.catalog.module
    if entrypoint:
        try:
            base = project_path.parent if project_path is not None else Path.cwd()
            catalog = load_catalog_entrypoint(entrypoint, base_dir=base)
            catalog.validate()
            print(f"catalog: {len(catalog)} metrics from {entrypoint}")
            print("dependency graph: acyclic OK")
            print(f"catalog semantic_hash: {catalog.semantic_hash()[:12]}…")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"catalog load/validate failed: {exc}")
    else:
        print("catalog: (none configured)")

    schedules = project.runtime.schedules
    if schedules:
        known = set(catalog.ids()) if catalog is not None else set()
        unknown = sorted(set(schedules) - known)
        if unknown:
            errors.append(f"runtime.schedules references unknown metric id(s): {unknown}")
        disabled = sorted(m for m, s in schedules.items() if not s.enabled)
        print(f"schedules: {len(schedules)} override(s), {len(disabled)} disabled")
    print(
        f"schedule: every {project.runtime.evaluation_interval} "
        f"(lag {project.runtime.evaluation_lag}, "
        f"catch-up {project.runtime.max_catchup_windows} window(s))"
    )

    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1

    print("validation OK")
    return 0


def _cmd_connections_test(args: argparse.Namespace) -> int:
    from metric_runtime.config.factory import build_runtime
    from metric_runtime.exceptions import MetricRuntimeError

    try:
        engine = build_runtime(
            args.profile,
            project_config=args.project_config,
            connections_config=args.connections,
        )
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if engine.executor is None:
        print("no metric_source configured for profile")
        return 1
    if hasattr(engine.executor, "ping"):
        engine.executor.ping()
        print("connection OK")
        return 0
    print("executor has no ping(); assuming OK")
    return 0


def _parse_timestamp(value: str, *, flag: str):
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{flag} expects an ISO-8601 timestamp, got {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _evaluate_one(args: argparse.Namespace, metric: str) -> int:
    from metric_runtime.config.factory import build_runtime
    from metric_runtime.exceptions import MetricRuntimeError

    if not args.at:
        print("error: pass --at ISO timestamp", file=sys.stderr)
        return 1
    try:
        at = _parse_timestamp(args.at, flag="--at")
        engine = build_runtime(
            args.profile,
            project_config=args.project_config,
            connections_config=args.connections,
        )
    except (MetricRuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if engine.executor is None:
        print("error: profile has no metric_source", file=sys.stderr)
        return 1
    try:
        status = engine.evaluate(metric, at)
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"{status.name}: value={status.value:.4g} anomaly={status.anomaly} z={status.z_score:+.2f}"
    )
    return 0


def _print_report(report) -> None:
    print(f"run_once now={report.now.isoformat()} · {report.summary()}")
    for outcome in report.outcomes:
        line = f"  {outcome.status:<10} {outcome.metric} @ {outcome.at.isoformat()}"
        if outcome.transition:
            line += f" ({outcome.transition})"
        if outcome.error and outcome.status == "failed":
            line += f" error={outcome.error}"
        print(line)
    for metric, count in report.catchup_skipped.items():
        print(f"  catch-up skipped {count} window(s) for {metric}")
    if report.next_metric_due_at is not None:
        print(f"next metric due at: {report.next_metric_due_at.isoformat()}")
    if report.next_outbox_due_at is not None:
        print(f"next outbox retry at: {report.next_outbox_due_at.isoformat()}")


def _cmd_run(args: argparse.Namespace) -> int:
    from metric_runtime.config.factory import build_metric_runtime
    from metric_runtime.exceptions import MetricRuntimeError

    if args.evaluate:
        print(
            "note: 'run --evaluate' is deprecated; use 'metric-runtime evaluate METRIC --at TS'",
            file=sys.stderr,
        )
        return _evaluate_one(args, args.evaluate)
    if args.now and not args.once:
        print("error: --now is only valid with --once", file=sys.stderr)
        return 1

    try:
        now = _parse_timestamp(args.now, flag="--now") if args.now else None
        runtime = build_metric_runtime(
            args.profile,
            project_config=args.project_config,
            connections_config=args.connections,
        )
    except (MetricRuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        runtime.ensure_ready()
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        runtime.close()
        return 1

    if args.once:
        try:
            report = runtime.run_once(now)
        except MetricRuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            runtime.close()
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            _print_report(report)
        return 0 if report.ok else 1

    import threading

    from metric_runtime.runtime import install_signal_handlers

    stop = threading.Event()
    install_signal_handlers(stop)
    print(
        f"metric-runtime running · metrics={len(runtime.scheduled_metrics())} "
        f"· idle_interval={runtime.schedule.idle_interval} (Ctrl+C to stop)",
        file=sys.stderr,
    )
    runtime.run_forever(stop, max_cycles=args.max_cycles)
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    return _evaluate_one(args, args.metric)


def _store_for(args: argparse.Namespace) -> tuple[RuntimeStore, str]:
    from metric_runtime.config.factory import build_profile_runtime_store

    return build_profile_runtime_store(
        args.profile,
        project_config=args.project_config,
        connections_config=args.connections,
    )


def _cmd_store_migrate(args: argparse.Namespace) -> int:
    from metric_runtime.exceptions import MetricRuntimeError
    from metric_runtime.stores.base import ManagedRuntimeStore

    try:
        store, profile = _store_for(args)
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if not isinstance(store, ManagedRuntimeStore):
            print(f"profile {profile}: runtime store has no versioned schema; nothing to migrate")
            return 0
        namespace = store.namespace
        applied = store.migrate()
    except Exception as exc:  # noqa: BLE001 - surface DB errors without a traceback
        print(f"error: migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    if applied:
        for m in applied:
            print(f"applied {m.filename}")
        print(f"{namespace}: {len(applied)} migration(s) applied")
    else:
        print(f"{namespace}: up to date")
    return 0


def _cmd_store_status(args: argparse.Namespace) -> int:
    from metric_runtime.exceptions import MetricRuntimeError
    from metric_runtime.stores.base import ManagedRuntimeStore

    try:
        store, profile = _store_for(args)
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if not isinstance(store, ManagedRuntimeStore):
            print(f"profile {profile}: runtime store has no versioned schema (not durable)")
            return 0
        status = store.schema_status()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"profile: {profile}")
    print(f"namespace: {status.namespace}")
    for version, (name, _checksum) in sorted(status.applied.items()):
        print(f"  applied {version:03d}_{name}")
    for m in status.pending:
        print(f"  pending {m.filename}")
    if status.pending:
        print(f"{len(status.pending)} pending migration(s): run 'metric-runtime store migrate'")
        return 1 if args.check else 0
    print("up to date")
    return 0


def _cmd_catalog_list(args: argparse.Namespace) -> int:
    from metric_runtime.exceptions import MetricRuntimeError

    try:
        catalog, _ = _load_project_catalog(args)
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        rows = [
            {
                "id": m.id,
                "name": m.name,
                "unit": m.unit.id,
                "owner": m.owner,
                "calculation": getattr(m.calculation, "kind", None),
            }
            for m in catalog.to_snapshot().metrics
        ]
        print(json.dumps(rows, indent=2))
        return 0

    print(f"{'ID':<28} {'NAME':<28} {'UNIT':<12} {'OWNER':<16} KIND")
    for m in catalog.to_snapshot().metrics:
        kind = getattr(m.calculation, "kind", "?")
        print(f"{m.id:<28} {m.name[:28]:<28} {m.unit.id:<12} {(m.owner or '-')[:16]:<16} {kind}")
    return 0


def _cmd_catalog_show(args: argparse.Namespace) -> int:
    from metric_runtime.exceptions import MetricRuntimeError

    try:
        catalog, _ = _load_project_catalog(args)
        metric = catalog.get(args.metric_id)
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(metric.model_dump_json(indent=2))
        return 0

    calc = metric.calculation
    kind = getattr(calc, "kind", None)
    print(f"id:            {metric.id}")
    print(f"name:          {metric.name}")
    print(f"description:   {metric.description or '-'}")
    print(f"calculation:   {kind}")
    print(f"dependencies:  {', '.join(metric.dependencies) or '-'}")
    print(f"dimensions:    {', '.join(metric.dimensions) or '-'}")
    print(f"unit:          {metric.unit.id}")
    print(f"owner:         {metric.owner or '-'}")
    print(f"detector:      {metric.detector.type}")
    print(f"directionality:{metric.directionality.value}")
    print(f"tags:          {', '.join(metric.tags) or '-'}")
    print(f"semantic_hash: {metric.semantic_hash()}")
    if args.verbose and kind == "sql":
        print("query:")
        print(getattr(calc, "query", ""))
    return 0


def _cmd_catalog_export(args: argparse.Namespace) -> int:
    from metric_runtime.exceptions import MetricRuntimeError

    try:
        catalog, _ = _load_project_catalog(args)
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.format == "jsonl":
        text = catalog.to_jsonl()
    else:
        text = catalog.to_json()

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(text)
    return 0


def _cmd_catalog_schema(args: argparse.Namespace) -> int:
    from metric_runtime.catalog import MetricCatalog
    from metric_runtime.models import Metric

    if args.metric:
        schema = Metric.model_json_schema()
    else:
        schema = MetricCatalog.json_schema()
    text = json.dumps(schema, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(text)
    return 0


def _cmd_catalog_diff(args: argparse.Namespace) -> int:
    from metric_runtime.catalog import MetricCatalog

    try:
        old = MetricCatalog.read_json(args.old)
        new = MetricCatalog.read_json(args.new)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    diff = old.diff(new)
    if args.format == "json":
        print(diff.model_dump_json(indent=2))
        return 0

    for mid in diff.added:
        print(f"+ {mid}")
    for mid in diff.removed:
        print(f"- {mid}")
    for mid, fields in diff.changed.items():
        print(f"~ {mid} ({', '.join(fields)})")
    if not diff.added and not diff.removed and not diff.changed:
        print("(no differences)")
    return 0


def _cmd_catalog_docs(args: argparse.Namespace) -> int:
    from metric_runtime.exceptions import MetricRuntimeError

    try:
        catalog, _ = _load_project_catalog(args)
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    text = catalog.to_markdown()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metric-runtime",
        description=("Define, validate, execute and operationalize semantic business metrics."),
    )
    # SUPPRESS so nested subcommands sharing these flags don't reset each other;
    # main() fills in the defaults.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--project-config",
        default=argparse.SUPPRESS,
        help="Path to metric-runtime.yaml",
    )
    shared.add_argument(
        "--connections",
        default=argparse.SUPPRESS,
        help="Path to connections.yaml",
    )
    shared.add_argument(
        "--profile",
        default=argparse.SUPPRESS,
        help="Runtime profile name (default: runtime.default_profile)",
    )
    shared.add_argument(
        "--log-level",
        default=argparse.SUPPRESS,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        type=str.upper,
        help="Logging level (default: INFO for run, WARNING otherwise)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_show = sub.add_parser("config", help="Inspect configuration", parents=[shared])
    config_sub = p_show.add_subparsers(dest="config_command", required=True)
    show = config_sub.add_parser("show", help="Show resolved non-secret config", parents=[shared])
    show.set_defaults(func=_cmd_config_show)

    p_val = sub.add_parser(
        "validate",
        help="Validate project config + metric catalog offline (no warehouse I/O)",
        parents=[shared],
    )
    p_val.add_argument(
        "--offline-catalog-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p_val.set_defaults(func=_cmd_validate)

    p_conn = sub.add_parser("connections", help="Connection operations", parents=[shared])
    conn_sub = p_conn.add_subparsers(dest="connections_command", required=True)
    test = conn_sub.add_parser("test", help="Test connectivity for a profile", parents=[shared])
    test.set_defaults(func=_cmd_connections_test)

    p_run = sub.add_parser(
        "run",
        help="Evaluate due metrics and deliver notifications (continuous, or --once)",
        parents=[shared],
    )
    p_run.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit (0 = all OK, 1 = a metric failed / config invalid)",
    )
    p_run.add_argument(
        "--now",
        default=None,
        help="ISO timestamp to schedule against (with --once; default: current time)",
    )
    p_run.add_argument("--json", action="store_true", help="Print the --once report as JSON")
    p_run.add_argument("--max-cycles", type=int, default=None, help=argparse.SUPPRESS)
    p_run.add_argument(
        "--evaluate",
        default=None,
        help="Deprecated: evaluate one metric (use 'metric-runtime evaluate')",
    )
    p_run.add_argument("--at", default=None, help="ISO timestamp for --evaluate")
    p_run.set_defaults(func=_cmd_run)

    p_eval = sub.add_parser("evaluate", help="Evaluate a metric via profile", parents=[shared])
    p_eval.add_argument("metric")
    p_eval.add_argument("--at", required=True)
    p_eval.set_defaults(func=_cmd_evaluate)

    p_store = sub.add_parser("store", help="Runtime store operations", parents=[shared])
    store_sub = p_store.add_subparsers(dest="store_command", required=True)
    s_migrate = store_sub.add_parser(
        "migrate", help="Apply pending runtime store migrations", parents=[shared]
    )
    s_migrate.set_defaults(func=_cmd_store_migrate)
    s_status = store_sub.add_parser(
        "status", help="Show applied / pending runtime store migrations", parents=[shared]
    )
    s_status.add_argument("--check", action="store_true", help="Exit 1 when migrations are pending")
    s_status.set_defaults(func=_cmd_store_status)

    p_cat = sub.add_parser("catalog", help="Inspect / export the metric catalog", parents=[shared])
    cat_sub = p_cat.add_subparsers(dest="catalog_command", required=True)

    c_list = cat_sub.add_parser("list", help="List metrics", parents=[shared])
    c_list.add_argument("--format", choices=("table", "json"), default="table")
    c_list.set_defaults(func=_cmd_catalog_list)

    c_show = cat_sub.add_parser("show", help="Show one metric", parents=[shared])
    c_show.add_argument("metric_id")
    c_show.add_argument("--format", choices=("text", "json"), default="text")
    c_show.add_argument("--verbose", action="store_true")
    c_show.set_defaults(func=_cmd_catalog_show)

    c_export = cat_sub.add_parser("export", help="Export catalog JSON/JSONL", parents=[shared])
    c_export.add_argument("--format", choices=("json", "jsonl"), default="json")
    c_export.add_argument("--output", "-o", default=None)
    c_export.set_defaults(func=_cmd_catalog_export)

    c_schema = cat_sub.add_parser("schema", help="Emit Metric/catalog JSON Schema")
    c_schema.add_argument("--metric", action="store_true", help="Emit Metric schema only")
    c_schema.add_argument("--output", "-o", default=None)
    c_schema.set_defaults(func=_cmd_catalog_schema)

    c_diff = cat_sub.add_parser("diff", help="Diff two catalog JSON snapshots")
    c_diff.add_argument("old")
    c_diff.add_argument("new")
    c_diff.add_argument("--format", choices=("text", "json"), default="text")
    c_diff.set_defaults(func=_cmd_catalog_diff)

    c_docs = cat_sub.add_parser("docs", help="Generate Markdown catalog docs", parents=[shared])
    c_docs.add_argument("--output", "-o", default=None)
    c_docs.set_defaults(func=_cmd_catalog_docs)

    return parser


_SHARED_DEFAULTS = {
    "project_config": None,
    "connections": None,
    "profile": None,
    "log_level": None,
}


def _configure_logging(args: argparse.Namespace) -> None:
    import logging

    level_name = args.log_level or ("INFO" if args.command == "run" else "WARNING")
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for key, value in _SHARED_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    _configure_logging(args)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
