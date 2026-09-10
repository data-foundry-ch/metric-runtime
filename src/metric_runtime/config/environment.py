"""Environment variable placeholder resolution."""

from __future__ import annotations

import os
import re
from typing import Any

from metric_runtime.exceptions import MissingEnvironmentVariableError

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def resolve_environment_variables(
    value: Any,
    *,
    context: str = "configuration",
    env: dict[str, str] | None = None,
) -> Any:
    """Resolve ``${VAR}`` placeholders. Never returns secrets into logs itself."""
    environ = env if env is not None else dict(os.environ)

    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in environ or environ[name] == "":
                raise MissingEnvironmentVariableError(
                    f"Missing environment variable {name} required by {context}."
                )
            return environ[name]

        return _ENV_PATTERN.sub(repl, value)

    if isinstance(value, dict):
        return {
            k: resolve_environment_variables(v, context=context, env=environ)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [resolve_environment_variables(v, context=context, env=environ) for v in value]
    return value


def find_env_placeholders(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(_ENV_PATTERN.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            found.update(find_env_placeholders(v))
    elif isinstance(value, list):
        for v in value:
            found.update(find_env_placeholders(v))
    return found
