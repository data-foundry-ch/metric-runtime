"""metric-runtime CLI.

Python API first. Configuration files for deployment.
"""

from __future__ import annotations

import argparse
import json
import sys


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
    import networkx as nx

    from metric_runtime.config.environment import find_env_placeholders
    from metric_runtime.config.factory import _load_catalog_from_module, resolve_profile
    from metric_runtime.config.loader import (
        DEFAULT_CONNECTIONS_FILENAMES,
        _read_yaml,
        discover_config_path,
        load_connections_config,
        load_project_config,
    )
    from metric_runtime.exceptions import MetricRuntimeError
    from metric_runtime.graph import build_business_graph

    errors: list[str] = []
    try:
        project, project_path = load_project_config(args.project_config)
        print(f"project config: {project_path or '(defaults)'}")
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        # Validate schema without requiring env vars first.
        connections, connections_path = load_connections_config(args.connections, resolve_env=False)
        print(f"connections config: {connections_path or '(none)'}")
    except MetricRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    profile_name = args.profile or project.runtime.default_profile
    try:
        profile = resolve_profile(connections, profile_name, project)
        print(f"profile: {profile_name}")
    except MetricRuntimeError as exc:
        errors.append(str(exc))
        profile = None

    if profile is not None and profile.metric_source:
        try:
            connections.get_connection(profile.metric_source)
        except MetricRuntimeError as exc:
            errors.append(str(exc))

    # Check env placeholders exist without printing values.
    path = discover_config_path(args.connections, defaults=DEFAULT_CONNECTIONS_FILENAMES)
    if path is not None:
        raw = _read_yaml(path)
        missing = []
        import os

        for name in sorted(find_env_placeholders(raw)):
            if not os.environ.get(name):
                missing.append(name)
        if missing:
            errors.append(
                "Missing environment variables required by connections.yaml: " + ", ".join(missing)
            )

    catalog = None
    if project.catalog.module:
        try:
            from pathlib import Path as _Path

            base = project_path.parent if project_path is not None else _Path.cwd()
            catalog = _load_catalog_from_module(project.catalog.module, base_dir=base)
            print(f"catalog: {len(catalog)} KPIs from {project.catalog.module}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"catalog load failed: {exc}")

    if catalog is not None:
        g = build_business_graph(catalog.as_dict())
        if not nx.is_directed_acyclic_graph(g):
            errors.append("KPI dependency graph contains a cycle")
        else:
            print("dependency graph: acyclic OK")

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

    print(f"engine ready · profile={args.profile} · kpis={len(engine.catalog)}")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metric-runtime",
        description="Executable semantics for business metrics.",
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
    show = config_sub.add_parser(
        "show", help="Show resolved non-secret config", parents=[shared]
    )
    show.set_defaults(func=_cmd_config_show)

    p_val = sub.add_parser(
        "validate", help="Validate config + catalog (no DB I/O)", parents=[shared]
    )
    p_val.set_defaults(func=_cmd_validate)

    p_conn = sub.add_parser("connections", help="Connection operations", parents=[shared])
    conn_sub = p_conn.add_subparsers(dest="connections_command", required=True)
    test = conn_sub.add_parser(
        "test", help="Test connectivity for a profile", parents=[shared]
    )
    test.set_defaults(func=_cmd_connections_test)

    p_run = sub.add_parser("run", help="Build runtime from profile", parents=[shared])
    p_run.add_argument("--evaluate", default=None, help="Optional KPI name to evaluate")
    p_run.add_argument("--at", default=None, help="ISO timestamp for --evaluate")
    p_run.set_defaults(func=_cmd_run)

    p_eval = sub.add_parser(
        "evaluate", help="Evaluate a KPI via profile", parents=[shared]
    )
    p_eval.add_argument("metric")
    p_eval.add_argument("--at", required=True)
    p_eval.set_defaults(func=_cmd_evaluate)
    return parser


def _cmd_evaluate(args: argparse.Namespace) -> int:
    args.evaluate = args.metric
    return _cmd_run(args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Propagate top-level config flags into subcommands that need them.
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
