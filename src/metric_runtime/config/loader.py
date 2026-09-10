"""Load project and connections configuration files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from metric_runtime.config.environment import resolve_environment_variables
from metric_runtime.config.models import ConnectionsFile, MetricRuntimeProjectConfig
from metric_runtime.exceptions import ConfigurationError

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


DEFAULT_PROJECT_FILENAMES = ("metric-runtime.yaml", "metric_runtime.yaml")
DEFAULT_CONNECTIONS_FILENAMES = ("connections.yaml",)


def _require_yaml() -> Any:
    if yaml is None:
        raise ConfigurationError(
            "PyYAML is required to load configuration files. "
            'Install with: pip install "metric-runtime[yaml]" or pip install pyyaml'
        )
    return yaml


def _read_yaml(path: Path) -> dict[str, Any]:
    y = _require_yaml()
    try:
        data = y.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(f"Failed to parse YAML file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"YAML root in {path} must be a mapping")
    return data


def discover_config_path(
    explicit: str | Path | None,
    *,
    defaults: tuple[str, ...],
    start: Path | None = None,
) -> Path | None:
    if explicit is not None:
        path = Path(explicit)
        if not path.exists():
            raise ConfigurationError(f"Configuration file not found: {path}")
        return path
    cwd = start or Path.cwd()
    for name in defaults:
        candidate = cwd / name
        if candidate.exists():
            return candidate
    return None


def load_project_config(
    path: str | Path | None = None,
    *,
    start: Path | None = None,
) -> tuple[MetricRuntimeProjectConfig, Path | None]:
    resolved = discover_config_path(path, defaults=DEFAULT_PROJECT_FILENAMES, start=start)
    if resolved is None:
        return MetricRuntimeProjectConfig(), None
    raw = _read_yaml(resolved)
    try:
        return MetricRuntimeProjectConfig.model_validate(raw), resolved
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(
            f"Invalid metric-runtime project config in {resolved}: {exc}"
        ) from exc


def load_connections_config(
    path: str | Path | None = None,
    *,
    start: Path | None = None,
    resolve_env: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[ConnectionsFile, Path | None]:
    resolved = discover_config_path(path, defaults=DEFAULT_CONNECTIONS_FILENAMES, start=start)
    if resolved is None:
        return ConnectionsFile(), None
    raw = _read_yaml(resolved)
    if resolve_env:
        # Resolve per-connection so error messages name the connection.
        connections = raw.get("connections") or {}
        resolved_connections: dict[str, Any] = {}
        for name, cfg in connections.items():
            resolved_connections[name] = resolve_environment_variables(
                cfg,
                context=f'connection "{name}"',
                env=env,
            )
        raw = {**raw, "connections": resolved_connections}
    try:
        return ConnectionsFile.model_validate(raw), resolved
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(f"Invalid connections config in {resolved}: {exc}") from exc


def redact_secrets(data: Any) -> Any:
    """Redact values that look like secrets for diagnostic output."""
    secret_keys = {
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "private_key",
        "access_key",
    }
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if str(k).lower() in secret_keys or str(k).lower().endswith("_password"):
                out[k] = "***REDACTED***"
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(data, list):
        return [redact_secrets(v) for v in data]
    return data
