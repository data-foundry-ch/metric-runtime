"""Wire project + connections config into a KPIEngine."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from metric_runtime.catalog import KPICatalog, MetricCatalog
from metric_runtime.config.duration import parse_duration, parse_duration_minutes
from metric_runtime.config.loader import (
    load_connections_config,
    load_project_config,
    redact_secrets,
)
from metric_runtime.config.models import (
    ConnectionsFile,
    DuckDBConnectionConfig,
    InlineMemoryStateStore,
    InlineNotifier,
    MemoryStateStoreConfig,
    MetricRuntimeProjectConfig,
    PostgresConnectionConfig,
    ProfileConfig,
    WebhookConnectionConfig,
)
from metric_runtime.engine import KPIEngine
from metric_runtime.exceptions import ConfigurationError, MetricRuntimeError
from metric_runtime.models import KPI, Metric
from metric_runtime.notifications.base import LoggingNotifier, Notifier, NullNotifier
from metric_runtime.notifications.delivery import NotificationPolicy
from metric_runtime.state import StatePolicy
from metric_runtime.stores.base import RuntimeStore
from metric_runtime.stores.memory import InMemoryRuntimeStore

if TYPE_CHECKING:
    from metric_runtime.runtime import MetricRuntime


def _ensure_sys_path(base_dir: Path | None) -> None:
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
            return
    for root in roots:
        candidate = str(root)
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
            return


def _coerce_catalog(obj: Any, *, source: str) -> MetricCatalog:
    if isinstance(obj, MetricCatalog):
        return obj
    if isinstance(obj, dict):
        return MetricCatalog(obj)
    if isinstance(obj, (list, tuple)):
        return MetricCatalog(list(obj))
    raise ConfigurationError(
        f"Catalog entrypoint {source!r} must resolve to a MetricCatalog "
        f"(or list/dict of Metric), got {type(obj)!r}"
    )


def load_catalog_entrypoint(
    entrypoint: str,
    *,
    base_dir: Path | None = None,
) -> MetricCatalog:
    """Load a MetricCatalog from ``python.module.path:attribute`` or module.

    Supported shapes:
    - ``metrics.catalog:catalog`` → attribute ``catalog`` on module
    - ``metrics.catalog`` → ``build_catalog()`` or ``CATALOG`` on module
    """
    _ensure_sys_path(base_dir)
    module_path, _, raw_attr = entrypoint.partition(":")
    module_path = module_path.strip()
    attr: str | None = raw_attr.strip() or None
    if not module_path:
        raise ConfigurationError(f"Invalid catalog entrypoint: {entrypoint!r}")

    try:
        module = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(f"Cannot import catalog module {module_path!r}: {exc}") from exc

    if attr:
        if not hasattr(module, attr):
            raise ConfigurationError(
                f"Catalog entrypoint {entrypoint!r}: attribute {attr!r} not found "
                f"on module {module_path!r}"
            )
        return _coerce_catalog(getattr(module, attr), source=entrypoint)

    if hasattr(module, "build_catalog"):
        return _coerce_catalog(module.build_catalog(), source=f"{module_path}.build_catalog()")
    if hasattr(module, "CATALOG"):
        return _coerce_catalog(module.CATALOG, source=f"{module_path}.CATALOG")
    if hasattr(module, "catalog"):
        return _coerce_catalog(module.catalog, source=f"{module_path}.catalog")
    raise ConfigurationError(
        f"Catalog module {module_path!r} must expose build_catalog(), CATALOG, "
        "catalog, or use entrypoint form 'module.path:attribute'"
    )


def _load_catalog_from_module(
    module_path: str,
    *,
    base_dir: Path | None = None,
) -> MetricCatalog:
    """Back-compat wrapper — prefer :func:`load_catalog_entrypoint`."""
    return load_catalog_entrypoint(module_path, base_dir=base_dir)


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
    conn: DuckDBConnectionConfig | PostgresConnectionConfig,
    project: MetricRuntimeProjectConfig,
    *,
    base_dir: Path | None = None,
):
    fact_table = conn.fact_table or project.runtime.fact_table
    if isinstance(conn, PostgresConnectionConfig):
        from metric_runtime.execution.postgres import PostgresExecutor

        timeout = parse_duration(conn.statement_timeout, field="statement_timeout")
        try:
            return PostgresExecutor(
                conn.conninfo(),
                fact_table=fact_table,
                timestamp_column=conn.timestamp_column,
                statement_timeout_ms=int(timeout.total_seconds() * 1000),
                connect_kwargs=conn.connect_kwargs(),
            )
        except ValueError as exc:
            raise ConfigurationError(f"Invalid postgres metric_source: {exc}") from exc

    from metric_runtime.execution.duckdb import DuckDBExecutor

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


def build_runtime_store(profile: ProfileConfig, connections) -> RuntimeStore:
    """Build the profile's runtime store (in-memory or Postgres).

    The Postgres store is returned unmigrated; callers that run evaluations
    should call ``ensure_migrated()`` (``metric-runtime store migrate`` applies).
    """
    store_ref = profile.runtime_store
    if store_ref is None or isinstance(store_ref, InlineMemoryStateStore):
        return InMemoryRuntimeStore()
    cfg = connections.get_connection(store_ref)
    if isinstance(cfg, MemoryStateStoreConfig):
        return InMemoryRuntimeStore()
    if isinstance(cfg, PostgresConnectionConfig):
        from metric_runtime.stores.postgres import PostgresRuntimeStore

        return PostgresRuntimeStore(
            cfg.conninfo(),
            schema=cfg.schema_name,
            min_size=cfg.pool_min_size,
            max_size=cfg.pool_max_size,
            claim_ttl=parse_duration(cfg.claim_ttl, field="claim_ttl").total_seconds(),
            connect_kwargs=cfg.connect_kwargs(),
        )
    raise ConfigurationError(
        f"runtime_store connection {store_ref!r} has type {cfg.type!r}; "
        "use type: postgres (durable) or type: memory"
    )


# Back-compat name.
build_state_store = build_runtime_store


_SOURCE_TYPES = {"duckdb", "postgres"}
_RUNTIME_STORE_TYPES = {"memory", "postgres"}
_NOTIFIER_TYPES = {"webhook"}


def validate_profile_wiring(
    profile: ProfileConfig,
    connections: ConnectionsFile,
    *,
    check_fields: bool = True,
) -> list[str]:
    """Role checks for a profile; never opens a connection.

    With ``check_fields=False`` only the declared connection ``type`` is
    checked, so unresolved ``${ENV}`` placeholders do not matter (offline
    ``validate``).
    """
    errors: list[str] = []

    def check(role: str, name: str, allowed: set[str]) -> None:
        if name not in connections.connections:
            errors.append(f"{role}: unknown connection {name!r}")
            return
        ctype = connections.connections[name].get("type")
        if ctype not in allowed:
            hint = (
                "DuckDB is an analytical source only; use type: postgres or type: memory"
                if role == "runtime_store" and ctype == "duckdb"
                else f"expected one of {sorted(allowed)}"
            )
            errors.append(f"{role} {name!r} has type {ctype!r}: {hint}")
            return
        if check_fields:
            try:
                connections.get_connection(name)
            except MetricRuntimeError as exc:
                errors.append(f"{role}: {exc}")

    if profile.metric_source is not None:
        check("metric_source", profile.metric_source, _SOURCE_TYPES)
    if isinstance(profile.runtime_store, str):
        check("runtime_store", profile.runtime_store, _RUNTIME_STORE_TYPES)
    if isinstance(profile.notifier, str):
        check("notifier", profile.notifier, _NOTIFIER_TYPES)
    if not errors:
        errors.extend(_shared_database_errors(profile, connections))
    return errors


def _shared_database_errors(profile: ProfileConfig, connections: ConnectionsFile) -> list[str]:
    """Runtime tables must not share a schema with the analytical source."""
    source_ref, store_ref = profile.metric_source, profile.runtime_store
    if not isinstance(source_ref, str) or not isinstance(store_ref, str):
        return []
    raw_source = connections.connections.get(source_ref, {})
    raw_store = connections.connections.get(store_ref, {})
    if raw_source.get("type") != "postgres" or raw_store.get("type") != "postgres":
        return []
    try:
        source = PostgresConnectionConfig.model_validate(raw_source)
        store = PostgresConnectionConfig.model_validate(raw_store)
    except ValueError:
        return []  # unresolved placeholders (offline validate); checked at build time
    same_db = source_ref == store_ref
    if not same_db:
        source_id, store_id = source.database_identity(), store.database_identity()
        same_db = source_id is not None and source_id == store_id
    if same_db and store.schema_name == source.source_schema:
        return [
            f"runtime_store {store_ref!r} and metric_source {source_ref!r} use the same "
            f"database and schema {store.schema_name!r}; give the runtime store its own "
            "schema (connection 'schema:') so runtime tables never mix with source data"
        ]
    return []


def build_notifier(profile: ProfileConfig, connections: ConnectionsFile) -> Notifier:
    """Build the profile's notifier (default: :class:`LoggingNotifier`)."""
    ref = profile.notifier
    if ref is None:
        return LoggingNotifier()
    if isinstance(ref, InlineNotifier):
        return NullNotifier() if ref.type == "none" else LoggingNotifier()
    cfg = connections.get_connection(ref)
    if not isinstance(cfg, WebhookConnectionConfig):
        raise ConfigurationError(f"notifier {ref!r} must be a type: webhook connection")
    from metric_runtime.notifications.webhook import WebhookNotifier

    return WebhookNotifier(
        cfg.url.get_secret_value(),
        secret=cfg.secret.get_secret_value() if cfg.secret else None,
        headers={k: v.get_secret_value() for k, v in cfg.headers.items()},
        timeout=cfg.timeout,
    )


@dataclass
class _ResolvedConfig:
    project: MetricRuntimeProjectConfig
    connections: ConnectionsFile
    profile_name: str
    profile: ProfileConfig
    base_dir: Path


def _resolve_config(
    profile: str | None,
    *,
    project_config: str | Path | None,
    connections_config: str | Path | None,
    start: Path | None,
) -> _ResolvedConfig:
    project, project_path = load_project_config(project_config, start=start)
    connections, connections_path = load_connections_config(connections_config, start=start)
    profile_name = profile or project.runtime.default_profile
    profile_cfg = resolve_profile(connections, profile_name, project)
    base_dir = (
        connections_path.parent
        if connections_path is not None
        else (project_path.parent if project_path is not None else Path.cwd())
    )
    return _ResolvedConfig(project, connections, profile_name, profile_cfg, base_dir)


def _build_engine(
    resolved: _ResolvedConfig,
    catalog: MetricCatalog | dict[str, Metric] | list[Metric] | None,
    *,
    notifier: Notifier | None = None,
) -> KPIEngine:
    project, connections, profile_cfg = resolved.project, resolved.connections, resolved.profile
    errors = validate_profile_wiring(profile_cfg, connections)
    if errors:
        raise ConfigurationError(f"Profile {resolved.profile_name!r}: " + "; ".join(errors))

    if catalog is None:
        entrypoint = project.catalog.entrypoint or project.catalog.module
        if entrypoint:
            catalog = load_catalog_entrypoint(entrypoint, base_dir=resolved.base_dir)
        else:
            catalog = MetricCatalog([])

    executor = None
    if profile_cfg.metric_source:
        conn = connections.get_connection(profile_cfg.metric_source)
        assert isinstance(conn, (DuckDBConnectionConfig, PostgresConnectionConfig))
        executor = build_executor_from_connection(conn, project, base_dir=resolved.base_dir)

    runtime_store = build_runtime_store(profile_cfg, connections)
    return KPIEngine(
        catalog=catalog,
        executor=executor,
        runtime_store=runtime_store,
        notifier=notifier or build_notifier(profile_cfg, connections),
        state_policy=state_policy_from_project(project),
        notification_policy=notification_policy_from_project(project),
    )


def build_runtime(
    profile: str | None,
    *,
    project_config: str | Path | None = None,
    connections_config: str | Path | None = None,
    catalog: MetricCatalog | dict[str, Metric] | list[Metric] | None = None,
    start: Path | None = None,
) -> KPIEngine:
    """Build a :class:`KPIEngine` for one profile (``None`` = default profile)."""
    resolved = _resolve_config(
        profile, project_config=project_config, connections_config=connections_config, start=start
    )
    return _build_engine(resolved, catalog)


def build_metric_runtime(
    profile: str | None,
    *,
    project_config: str | Path | None = None,
    connections_config: str | Path | None = None,
    catalog: MetricCatalog | dict[str, Metric] | list[Metric] | None = None,
    start: Path | None = None,
    notifier: Notifier | None = None,
) -> MetricRuntime:
    """Build the scheduled :class:`MetricRuntime` for one profile."""
    from metric_runtime.runtime import MetricRuntime, RuntimeSchedule

    resolved = _resolve_config(
        profile, project_config=project_config, connections_config=connections_config, start=start
    )
    engine = _build_engine(resolved, catalog, notifier=notifier)
    return MetricRuntime(
        engine,
        schedule=RuntimeSchedule.from_config(resolved.project.runtime),
    )


def build_profile_runtime_store(
    profile: str | None,
    *,
    project_config: str | Path | None = None,
    connections_config: str | Path | None = None,
    start: Path | None = None,
) -> tuple[RuntimeStore, str]:
    """Build only the runtime store of a profile (``store migrate`` / ``status``)."""
    resolved = _resolve_config(
        profile, project_config=project_config, connections_config=connections_config, start=start
    )
    store_only = resolved.profile.model_copy(update={"metric_source": None})
    errors = validate_profile_wiring(store_only, resolved.connections)
    if errors:
        raise ConfigurationError(f"Profile {resolved.profile_name!r}: " + "; ".join(errors))
    return build_runtime_store(resolved.profile, resolved.connections), resolved.profile_name


def notification_policy_from_project(project: MetricRuntimeProjectConfig) -> NotificationPolicy:
    cfg = project.runtime.notifications
    return NotificationPolicy(
        max_attempts=cfg.max_attempts,
        backoff_initial=parse_duration(cfg.backoff_initial, field="backoff_initial"),
        backoff_max=parse_duration(cfg.backoff_max, field="backoff_max"),
        lease=parse_duration(cfg.lease, field="lease"),
    )


def state_policy_from_project(project: MetricRuntimeProjectConfig) -> StatePolicy:
    minutes = parse_duration_minutes(project.state.cooldown, field="state.cooldown")
    return StatePolicy(
        detections_before_open=project.state.detections_before_open,
        resolve_after_healthy_windows=project.state.resolve_after_healthy_windows,
        cooldown_minutes=minutes,
        min_impact=project.state.min_impact,
    )


def show_resolved_config(
    profile: str | None,
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
    profile = profile or project.runtime.default_profile
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


# Re-export for callers still typing KPICatalog / KPI.
__all__ = [
    "KPI",
    "KPICatalog",
    "Metric",
    "MetricCatalog",
    "build_metric_runtime",
    "build_notifier",
    "build_runtime",
    "build_runtime_store",
    "load_catalog_entrypoint",
    "notification_policy_from_project",
    "resolve_profile",
    "show_resolved_config",
    "state_policy_from_project",
    "validate_profile_wiring",
]
