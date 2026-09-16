"""Safe derived-KPI expression evaluation.

Deliberately small AST whitelist — not unrestricted eval().
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Mapping
from typing import Any

from metric_runtime.calculations.context import CalculationResult, ObservationValueStatus
from metric_runtime.exceptions import InvalidMetricDefinitionError, MetricRuntimeError

_ALLOWED_BINOPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

_ALLOWED_UNARY: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_ALLOWED_FUNCTIONS = frozenset({"min", "max", "coalesce", "nullif"})


def expression_identifiers(expression: str) -> set[str]:
    """Return KPI identifiers referenced by a derived expression."""
    tree = _parse(expression)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
                raise InvalidMetricDefinitionError(
                    f"Unsupported function in derived expression: {ast.dump(node.func)}"
                )
    return names - _ALLOWED_FUNCTIONS


def evaluate_expression(
    expression: str,
    values: Mapping[str, CalculationResult | float | None],
) -> CalculationResult:
    """Evaluate a derived expression against dependency results.

    Division by zero and missing/NO_DATA dependencies yield NO_DATA
    (they do not crash the catalog evaluation).
    """
    resolved: dict[str, float | None] = {}
    for name, item in values.items():
        if isinstance(item, CalculationResult):
            if item.status == ObservationValueStatus.ERROR:
                return CalculationResult.from_error(
                    metric="",
                    error=f"dependency {name!r} failed: {item.error}",
                    source="derived",
                )
            if item.status != ObservationValueStatus.VALUE or item.value is None:
                resolved[name] = None
            else:
                resolved[name] = float(item.value)
        else:
            resolved[name] = None if item is None else float(item)

    try:
        tree = _parse(expression)
        value = _eval_node(tree.body, resolved)
    except _NoData:
        return CalculationResult.no_data(metric="", source="derived")
    except ZeroDivisionError:
        return CalculationResult.no_data(metric="", source="derived")
    except MetricRuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MetricRuntimeError(f"Failed to evaluate derived expression: {exc}") from exc

    if value is None:
        return CalculationResult.no_data(metric="", source="derived")
    return CalculationResult.from_value("", float(value), source="derived")


def _parse(expression: str) -> ast.Expression:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise InvalidMetricDefinitionError(
            f"Invalid derived expression syntax: {expression!r}"
        ) from exc
    _validate_ast(tree)
    return tree


def _validate_ast(tree: ast.AST) -> None:
    allowed_types = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.Call,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.UAdd,
        ast.USub,
    )
    for node in ast.walk(tree):
        if isinstance(node, allowed_types):
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
                    raise InvalidMetricDefinitionError(
                        f"Function not allowed in derived expression: {ast.dump(node)}"
                    )
                if node.keywords:
                    raise InvalidMetricDefinitionError(
                        "Keyword arguments are not allowed in derived expressions"
                    )
            continue
        raise InvalidMetricDefinitionError(
            f"Disallowed syntax in derived expression: {type(node).__name__}"
        )


class _NoData(Exception):
    pass


def _eval_node(node: ast.AST, values: dict[str, float | None]) -> float | None:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if node.value is None:
            return None
        raise MetricRuntimeError(f"Unsupported literal in derived expression: {node.value!r}")

    if isinstance(node, ast.Name):
        if node.id not in values:
            raise MetricRuntimeError(f"Unknown identifier in derived expression: {node.id!r}")
        value = values[node.id]
        if value is None:
            raise _NoData
        return value

    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_UNARY.get(type(node.op))
        if op is None:
            raise MetricRuntimeError(f"Unsupported unary operator: {type(node.op).__name__}")
        operand = _eval_node(node.operand, values)
        if operand is None:
            raise _NoData
        return float(op(operand))

    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise MetricRuntimeError(f"Unsupported operator: {type(node.op).__name__}")
        left = _eval_node(node.left, values)
        right = _eval_node(node.right, values)
        if left is None or right is None:
            raise _NoData
        if isinstance(node.op, ast.Div) and right == 0:
            raise ZeroDivisionError
        return float(op(left, right))

    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        args = [_eval_node(arg, values) for arg in node.args]
        return _call_function(node.func.id, args)

    raise MetricRuntimeError(f"Unsupported expression node: {type(node).__name__}")


def _call_function(name: str, args: list[float | None]) -> float | None:
    if name == "min":
        present = [a for a in args if a is not None]
        if not present:
            raise _NoData
        return float(min(present))
    if name == "max":
        present = [a for a in args if a is not None]
        if not present:
            raise _NoData
        return float(max(present))
    if name == "coalesce":
        for arg in args:
            if arg is not None:
                return float(arg)
        raise _NoData
    if name == "nullif":
        if len(args) != 2:
            raise MetricRuntimeError("nullif expects exactly two arguments")
        left, right = args
        if left is None or right is None:
            raise _NoData
        if left == right:
            raise _NoData
        return float(left)
    raise MetricRuntimeError(f"Unsupported function: {name}")
