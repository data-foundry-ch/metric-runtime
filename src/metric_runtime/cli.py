"""metric-runtime CLI.

Python API first. Configuration files for deployment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


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


def _cmd_validate(args: argparse.Namespace) -> int:
    from metric_runtime.config.environment import find_env_placeholders
    from metric_runtime.config.factory import load_catalog_entrypoint, resolve_profile
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
            resolve_profile(connections, profile_name, project)
            print(f"profile: {profile_name}")
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


def _cmd_run(args: argparse.Namespace) -> int:
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

    print(f"engine ready · profile={args.profile} · metrics={len(engine.catalog)}")
    if args.evaluate and engine.executor is not None:
        from datetime import datetime

        at = datetime.fromisoformat(args.at) if args.at else None
        if at is None:
            print("pass --at ISO timestamp with --evaluate")
            return 1
        status = engine.evaluate(args.evaluate, at)
        print(
            f"{status.name}: value={status.value:.4g} "
            f"anomaly={status.anomaly} z={status.z_score:+.2f}"
        )
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
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--project-config",
        default=None,
        help="Path to metric-runtime.yaml",
    )
    shared.add_argument(
        "--connections",
        default=None,
        help="Path to connections.yaml",
    )
    shared.add_argument(
        "--profile",
        default="local",
        help="Runtime profile name",
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

    p_run = sub.add_parser("run", help="Build runtime from profile", parents=[shared])
    p_run.add_argument("--evaluate", default=None, help="Optional metric id to evaluate")
    p_run.add_argument("--at", default=None, help="ISO timestamp for --evaluate")
    p_run.set_defaults(func=_cmd_run)

    p_eval = sub.add_parser("evaluate", help="Evaluate a metric via profile", parents=[shared])
    p_eval.add_argument("metric")
    p_eval.add_argument("--at", required=True)
    p_eval.set_defaults(func=_cmd_evaluate)

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


def _cmd_evaluate(args: argparse.Namespace) -> int:
    args.evaluate = args.metric
    return _cmd_run(args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
