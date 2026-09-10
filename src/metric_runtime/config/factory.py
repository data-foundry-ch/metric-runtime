"""Wire project + connections config into a KPIEngine."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from metric_runtime.catalog import KPICatalog
from metric_runtime.config.loader import (
    load_connections_config,
    load_project_config,
    redact_secrets,
)
from metric_runtime.config.models import (
    DuckDBConnectionConfig,
    InlineMemoryStateStore,
    MetricRuntimeProjectConfig,
    ProfileConfig,
)
from metric_runtime.engine import KPIEngine
from metric_runtime.exceptions import ConfigurationError, UnknownConnectionError
from metric_runtime.models import KPI
from metric_runtime.state import StatePolicy
from metric_runtime.stores.memory import InMemoryStateStore


def _load_catalog_from_module(
    module_path: str,
    *,
    base_dir: Path | None = None,
) -> KPICatalog:
    import sys

    roots: list[Path] = []
    if base_dir is not None:
        roots.append(base_dir)
        roots.append(base_dir.parent)
        roots.append(base_dir.parent.parent)
    roots.append(Path.cwd())
    for root in roots:
        marker = root / "pyproject.toml"
        candidate = str(root)
        if marker.exists() and candidate not in sys.path:
            sys.path.insert(0, candidate)
            break
    else:
        for root in roots:
            candidate = str(root)
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
                break

    module = importlib.import_module(module_path)
    if hasattr(module, "build_catalog"):
        built = module.build_catalog()
        if isinstance(built, KPICatalog):
            return built
        if isinstance(built, dict):
            return KPICatalog(built)
        return KPICatalog(list(built))
    if hasattr(module, "CATALOG"):
        return KPICatalog(module.CATALOG)
    raise ConfigurationError(
        f"Catalog module {module_path!r} must expose build_catalog() or CATALOG"
    )


def resolve_profile(
    connections,
    profile_name: str,
    project: MetricRuntimeProjectConfig,
) -> ProfileConfig:
    name = profile_name or project.runtime.default_profile
    if name not in connections.profiles:
        raise ConfigurationError(
            f"Profile {name!r} not found. Available: {sorted(connections.profiles)}"
        )
    return connections.profiles[name]


def build_executor_from_connection(
    conn: DuckDBConnectionConfig,
    project: MetricRuntimeProjectConfig,
    *,
    base_dir: Path | None = None,
):
    from metric_runtime.execution.duckdb import DuckDBExecutor

    fact_table = conn.fact_table or project.runtime.fact_table
    if not fact_table:
        raise ConfigurationError(
            "DuckDB connection requires fact_table "
            "(set connections.<name>.fact_table or runtime.fact_table "
            "in metric-runtime.yaml)."
        )
    if conn.path is None:
        raise ConfigurationError("DuckDB connection requires path")
    path = Path(conn.path)
    if not path.is_absolute() and base_dir is not None:
        path = (base_dir / path).resolve()
    return DuckDBExecutor(
        path,
        fact_table=fact_table,
        read_only=conn.read_only,
    )


def build_state_store(profile: ProfileConfig, connections) -> InMemoryStateStore:
    store_ref = profile.state_store
    if store_ref is None or isinstance(store_ref, InlineMemoryStateStore):
        return InMemoryStateStore()
    if isinstance(store_ref, str):
        cfg = connections.get_connection(store_ref)
        if cfg.type == "memory":
            return InMemoryStateStore()
        raise ConfigurationError(
            f"State store connection {store_ref!r} type {cfg.type!r} "
            "is not supported in v0.1 (use type: memory)"
        )
    return InMemoryStateStore()


def build_runtime(
    profile: str,
    *,
    project_config: str | Path | None = None,
    connections_config: str | Path | None = None,
    catalog: KPICatalog | dict[str, KPI] | list[KPI] | None = None,
    start: Path | None = None,
) -> KPIEngine:
    project, project_path = load_project_config(project_config, start=start)
    connections, connections_path = load_connections_config(connections_config, start=start)
    profile_cfg = resolve_profile(connections, profile, project)
    base_dir = (
        connections_path.parent
        if connections_path is not None
        else (project_path.parent if project_path is not None else Path.cwd())
    )

    if catalog is None:
        if project.catalog.module:
            catalog = _load_catalog_from_module(
                project.catalog.module,
                base_dir=base_dir,
            )
        else:
            catalog = KPICatalog([])

    executor = None
    if profile_cfg.metric_source:
        try:
            conn = connections.get_connection(profile_cfg.metric_source)
        except UnknownConnectionError:
            raise
        if isinstance(conn, DuckDBConnectionConfig):
            executor = build_executor_from_connection(conn, project, base_dir=base_dir)
        else:
            raise ConfigurationError(
                f"metric_source {profile_cfg.metric_source!r} must be a duckdb connection"
            )

    state_store = build_state_store(profile_cfg, connections)
    return KPIEngine(
        catalog=catalog,
        executor=executor,
        state_store=state_store,
        state_policy=state_policy_from_project(project),
    )


def state_policy_from_project(project: MetricRuntimeProjectConfig) -> StatePolicy:
    cooldown = project.state.cooldown
    minutes = 30
    if cooldown.endswith("m"):
        minutes = int(cooldown[:-1])
    elif cooldown.endswith("h"):
        minutes = int(cooldown[:-1]) * 60
    return StatePolicy(
        detections_before_open=project.state.detections_before_open,
        resolve_after_healthy_windows=project.state.resolve_after_healthy_windows,
        cooldown_minutes=minutes,
        min_impact_eur=project.state.min_impact_eur,
    )


def show_resolved_config(
    profile: str,
    *,
    project_config: str | Path | None = None,
    connections_config: str | Path | None = None,
    start: Path | None = None,
) -> dict[str, Any]:
    """Return resolved NON-SECRET configuration for inspection."""
    project, project_path = load_project_config(project_config, start=start)
    # Do not resolve env for display of missing vars as secrets; load raw then redact.
    from metric_runtime.config.loader import (
        DEFAULT_CONNECTIONS_FILENAMES,
        _read_yaml,
        discover_config_path,
    )

    connections_path = discover_config_path(
        connections_config, defaults=DEFAULT_CONNECTIONS_FILENAMES, start=start
    )
    raw_connections: dict[str, Any] = {}
    if connections_path is not None:
        raw_connections = _read_yaml(connections_path)

    connections, _ = load_connections_config(connections_config, start=start, resolve_env=False)
    profile_cfg = resolve_profile(connections, profile, project)

    return {
        "project_config_path": str(project_path) if project_path else None,
        "connections_config_path": str(connections_path) if connections_path else None,
        "profile": profile,
        "project": project.model_dump(mode="json"),
        "profile_wiring": profile_cfg.model_dump(mode="json"),
        "connections": redact_secrets(raw_connections.get("connections", {})),
        "profiles": {
            name: cfg.model_dump(mode="json") for name, cfg in connections.profiles.items()
        },
    }
