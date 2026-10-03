"""SQL helpers shared by executors (identifier quoting, ``:name`` parameters).

Metric Runtime does not transpile SQL across dialects; these helpers only
validate identifiers and locate the portable ``:name`` placeholders that
calculations use, so each executor can bind them in its driver's style.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from metric_runtime.exceptions import MetricRuntimeError

__all__ = [
    "bind_named_params",
    "named_params",
    "quote_identifier",
    "quote_relation",
]

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# ``:name`` but not ``::type`` casts.
PARAM_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def quote_identifier(name: str) -> str:
    """Validate and double-quote a SQL identifier.

    Rejects anything that is not a plain identifier so user-controlled
    strings cannot alter generated SQL structure.
    """
    if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
        raise ValueError(
            f"Invalid SQL identifier {name!r}. Expected a name like 'orders' or 'gross_revenue'."
        )
    return f'"{name}"'


def quote_relation(name: str) -> str:
    """Validate and quote a table reference (optionally schema-qualified)."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"Invalid relation name {name!r}.")
    parts = name.split(".")
    if any(not _IDENT_RE.fullmatch(part) for part in parts):
        raise ValueError(
            f"Invalid relation name {name!r}. "
            "Expected 'table' or 'schema.table' with plain identifiers."
        )
    return ".".join(quote_identifier(part) for part in parts)


def named_params(query: str) -> list[str]:
    """``:name`` placeholders referenced by ``query`` (first-seen order, unique)."""
    return list(dict.fromkeys(PARAM_RE.findall(query)))


def bind_named_params(
    query: str,
    parameters: dict[str, Any],
    *,
    placeholder: str,
    convert: Callable[[Any], Any] = lambda value: value,
) -> tuple[str, dict[str, Any]]:
    """Rewrite ``:name`` to the driver ``placeholder`` and bind referenced values.

    ``placeholder`` is a ``re.sub`` template using ``\\1`` for the name
    (``$\\1`` for DuckDB, ``%(\\1)s`` for psycopg). Only referenced parameters
    are bound; a missing one raises :class:`MetricRuntimeError`.
    """
    bound: dict[str, Any] = {}
    for name in named_params(query):
        if name not in parameters:
            raise MetricRuntimeError(f"Missing SQL parameter: {name!r}")
        bound[name] = convert(parameters[name])
    return PARAM_RE.sub(placeholder, query), bound
