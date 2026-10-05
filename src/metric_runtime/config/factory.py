"""Wire project + connections config into a KPIEngine."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from metric_runtime.adapters.base import AdapterContext, MetricRuntimeAdapter, Role
from metric_runtime.adapters.registry import adapters_supporting, get_adapter
from metric_runtime.catalog import KPICatalog, MetricCatalog
from metric_runtime.config.duration import parse_duration, parse_duration_minutes
from metric_runtime.config.loader import (
    load_connections_config,
    load_project_config,
    redact_connections,
)
from metric_runtime.config.models import (
    ConnectionsFile,
    InlineMemoryStateStore,
    InlineNotifier,
    MetricRuntimeProjectConfig,
    ProfileConfig,
)
from metric_runtime.engine import KPIEngine
from metric_runtime.exceptions import ConfigurationError, MetricRuntimeError, UnsupportedRoleError
from metric_runtime.execution.base import MetricExecutor
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


def _context(
    project: MetricRuntimeProjectConfig | None,
    base_dir: Path | None,
    connection_name: str | None = None,
) -> AdapterContext:
    return AdapterContext(
        project=project or MetricRuntimeProjectConfig(),
        base_dir=base_dir or Path.cwd(),
        connection_name=connection_name,
    )


def _adapter_for_role(connections: ConnectionsFile, name: str, role: Role) -> MetricRuntimeAdapter:
    """Resolve a connection's adapter and require that it supports ``role``."""
    ctype = connections.connection_type(name)
    adapter = get_adapter(ctype)
    if not adapter.capabilities.supports(role):
        raise UnsupportedRoleError(_unsupported_role_message(role, name, ctype, adapter))
    return adapter


def _unsupported_role_message(
    role: str, name: str, ctype: Any, adapter: MetricRuntimeAdapter
) -> str:
    hint_fn = getattr(adapter, "unsupported_role_hint", None)
    hint = hint_fn(role) if callable(hint_fn) else None
    reason = hint or f"the {ctype!r} adapter does not support the {role} role"
    supporting = ", ".join(adapters_supporting(role)) or "none registered"
    return (
        f"{role} {name!r} has type {ctype!r}: {reason} (adapters supporting {role}: {supporting})"
    )


def build_executor_from_connection(
    conn: BaseModel,
    project: MetricRuntimeProjectConfig,
    *,
    base_dir: Path | None = None,
    connection_name: str | None = None,
) -> MetricExecutor:
    """Build a ``metric_source`` executor from a validated connection config."""
    ctype = getattr(conn, "type", None)
    adapter = get_adapter(ctype)
    if not adapter.capabilities.supports("metric_source"):
        raise UnsupportedRoleError(
            _unsupported_role_message("metric_source", connection_name or "?", ctype, adapter)
        )
    return adapter.build_executor(conn, _context(project, base_dir, connection_name))


def build_runtime_store(
    profile: ProfileConfig,
    connections: ConnectionsFile,
    *,
    context: AdapterContext | None = None,
) -> RuntimeStore:
    """Build the profile's runtime store through its connection's adapter.

    No ``runtime_store`` (or an inline ``{type: memory}``) gives a
    process-local :class:`InMemoryRuntimeStore`. Stores with a versioned
    schema are returned unmigrated; ``metric-runtime store migrate`` applies.
    """
    store_ref = profile.runtime_store
    if store_ref is None or isinstance(store_ref, InlineMemoryStateStore):
        return InMemoryRuntimeStore()
    adapter = _adapter_for_role(connections, store_ref, "runtime_store")
    cfg = connections.get_connection(store_ref)
    ctx = context or _context(None, None)
    return adapter.build_runtime_store(cfg, replace(ctx, connection_name=store_ref))


# Back-compat name.
build_state_store = build_runtime_store


def validate_profile_wiring(
    profile: ProfileConfig,
    connections: ConnectionsFile,
    *,
    check_fields: bool = True,
) -> list[str]:
    """Role checks for a profile; never opens a connection.

    Each role is resolved independently: the connection's adapter must
    support it. When the profile has both a source and a runtime store, the
    runtime store's adapter decides whether runtime-owned objects could
    collide with source data. With ``check_fields=False`` connection fields
    are not required to validate (unresolved ``${ENV}`` placeholders do not
    matter for offline ``validate``).
    """
    errors: list[str] = []

    def check(role: Role, name: str) -> None:
        if name not in connections.connections:
            errors.append(f"{role}: unknown connection {name!r}")
            return
        try:
            _adapter_for_role(connections, name, role)
        except UnsupportedRoleError as exc:
            errors.append(str(exc))
            return
        except MetricRuntimeError as exc:
            errors.append(f"{role} {name!r}: {exc}")
            return
        if check_fields:
            try:
                connections.get_connection(name)
            except MetricRuntimeError as exc:
                errors.append(f"{role}: {exc}")

    if profile.metric_source is not None:
        check("metric_source", profile.metric_source)
    if isinstance(profile.runtime_store, str):
        check("runtime_store", profile.runtime_store)
    if isinstance(profile.notifier, str):
        check("notifier", profile.notifier)
    if not errors:
        errors.extend(_role_conflicts(profile, connections))
    return errors


def _role_conflicts(profile: ProfileConfig, connections: ConnectionsFile) -> list[str]:
    """Ask the runtime store's adapter whether its objects could collide with the source."""
    source_ref, store_ref = profile.metric_source, profile.runtime_store
    if not isinstance(source_ref, str) or not isinstance(store_ref, str):
        return []
    try:
        store_adapter = get_adapter(connections.connection_type(store_ref))
        source_cfg = connections.get_connection(source_ref)
        store_cfg = connections.get_connection(store_ref)
    except MetricRuntimeError:
        return []  # unresolved placeholders (offline validate); checked at build time
    conflicts = store_adapter.role_conflicts(
        source_type=connections.connection_type(source_ref),
        source_config=source_cfg,
        runtime_config=store_cfg,
        same_connection=source_ref == store_ref,
    )
    return [
        f"runtime_store {store_ref!r} / metric_source {source_ref!r}: {message}"
        for message in conflicts
    ]


def build_notifier(
    profile: ProfileConfig,
    connections: ConnectionsFile,
    *,
    context: AdapterContext | None = None,
) -> Notifier:
    """Build the profile's notifier (default: :class:`LoggingNotifier`)."""
    ref = profile.notifier
    if ref is None:
        return LoggingNotifier()
    if isinstance(ref, InlineNotifier):
        return NullNotifier() if ref.type == "none" else LoggingNotifier()
    adapter = _adapter_for_role(connections, ref, "notifier")
    cfg = connections.get_connection(ref)
    ctx = context or _context(None, None)
    return adapter.build_notifier(cfg, replace(ctx, connection_name=ref))


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

    # Each role gets its own resources, even when it uses the same connection.
    context = _context(project, resolved.base_dir)
    executor = None
    if profile_cfg.metric_source:
        source_ref = profile_cfg.metric_source
        adapter = _adapter_for_role(connections, source_ref, "metric_source")
        executor = adapter.build_executor(
            connections.get_connection(source_ref),
            replace(context, connection_name=source_ref),
        )

    runtime_store = build_runtime_store(profile_cfg, connections, context=context)
    return KPIEngine(
        catalog=catalog,
        executor=executor,
        runtime_store=runtime_store,
        notifier=notifier or build_notifier(profile_cfg, connections, context=context),
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
    store = build_runtime_store(
        resolved.profile,
        resolved.connections,
        context=_context(resolved.project, resolved.base_dir),
    )
    return store, resolved.profile_name


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
        "connections": redact_connections(raw_connections.get("connections") or {}),
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
    "build_executor_from_connection",
    "build_metric_runtime",
    "build_notifier",
    "build_profile_runtime_store",
    "build_runtime",
    "build_runtime_store",
    "build_state_store",
    "load_catalog_entrypoint",
    "notification_policy_from_project",
    "resolve_profile",
    "show_resolved_config",
    "state_policy_from_project",
    "validate_profile_wiring",
]
