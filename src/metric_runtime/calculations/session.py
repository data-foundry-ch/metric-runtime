"""Catalog-level evaluation session: shared batch work + topo order."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from metric_runtime.calculations.batch import (
    BatchRegistry,
    CallableBatchSource,
    SqlBatchSource,
)
from metric_runtime.calculations.context import (
    CalculationResult,
    EvaluationContext,
    ObservationValueStatus,
    ScalarValue,
)
from metric_runtime.calculations.expressions import (
    evaluate_expression,
    expression_identifiers,
)
from metric_runtime.calculations.specs import (
    BatchCalculation,
    DerivedCalculation,
    FormulaCalculation,
    SqlCalculation,
    merge_parameter_bindings,
)
from metric_runtime.catalog import KPICatalog
from metric_runtime.exceptions import (
    InvalidMetricDefinitionError,
    MetricRuntimeError,
    UnknownMetricError,
)
from metric_runtime.identity import ensure_utc
from metric_runtime.models import KPI, KPIObservation


class EvaluationPlan:
    """Deterministic plan for one catalog evaluation (testable, not required public)."""

    def __init__(
        self,
        *,
        requested: tuple[str, ...],
        closure: tuple[str, ...],
        ordered: tuple[str, ...],
        batch_sources: tuple[str, ...],
    ) -> None:
        self.requested = requested
        self.closure = closure
        self.ordered = ordered
        self.batch_sources = batch_sources


class EvaluationSession:
    """Coordinate formula / SQL / batch / derived calculations.

    Produces observations only — never detectors, state, incidents, or alerts.
    """

    def __init__(
        self,
        catalog: KPICatalog | Mapping[str, KPI],
        executor: Any | None = None,
        *,
        batch_registry: BatchRegistry | None = None,
        sql_dialects: frozenset[str] | None = None,
    ) -> None:
        self.catalog = catalog if isinstance(catalog, KPICatalog) else KPICatalog(catalog)
        self.executor = executor
        self.batch_registry = batch_registry or BatchRegistry()
        # None: accept whatever the executor declares in its own ``sql_dialects``.
        self.sql_dialects = sql_dialects
        self._batch_cache: dict[tuple[str, str], dict[str, float | None]] = {}
        self._validate_catalog_calculations()

    def plan(self, metric_names: Sequence[str]) -> EvaluationPlan:
        closure = self._dependency_closure(metric_names)
        ordered = self._topo_order(closure)
        batches: list[str] = []
        for name in ordered:
            calc = self._calculation_for(name)
            if isinstance(calc, BatchCalculation) and calc.source not in batches:
                batches.append(calc.source)
        return EvaluationPlan(
            requested=tuple(metric_names),
            closure=tuple(closure),
            ordered=tuple(ordered),
            batch_sources=tuple(batches),
        )

    def evaluate(
        self,
        metric_names: Sequence[str],
        context: EvaluationContext,
    ) -> dict[str, KPIObservation]:
        """Evaluate metrics (+ dependency closure) into KPIObservations."""
        plan = self.plan(metric_names)
        results: dict[str, CalculationResult] = {}
        for name in plan.ordered:
            results[name] = self._evaluate_metric(name, context, results)

        observations: dict[str, KPIObservation] = {}
        for name in plan.closure:
            result = results[name]
            observations[name] = self._observation_for(name, result, context)
        return {name: observations[name] for name in metric_names if name in observations}

    def evaluate_all(
        self,
        metric_names: Sequence[str],
        context: EvaluationContext,
    ) -> dict[str, KPIObservation]:
        """Like evaluate(), but return the full dependency closure."""
        plan = self.plan(metric_names)
        results: dict[str, CalculationResult] = {}
        for name in plan.ordered:
            results[name] = self._evaluate_metric(name, context, results)
        return {name: self._observation_for(name, results[name], context) for name in plan.closure}

    def calculate_value(
        self,
        metric_name: str,
        context: EvaluationContext,
    ) -> CalculationResult:
        """Evaluate one metric (and deps) and return the CalculationResult."""
        plan = self.plan([metric_name])
        results: dict[str, CalculationResult] = {}
        for name in plan.ordered:
            results[name] = self._evaluate_metric(name, context, results)
        return results[metric_name]

    def _evaluate_metric(
        self,
        name: str,
        context: EvaluationContext,
        prior: dict[str, CalculationResult],
    ) -> CalculationResult:
        calc = self._calculation_for(name)
        if isinstance(calc, FormulaCalculation):
            return self._eval_formula(name, calc, context)
        if isinstance(calc, SqlCalculation):
            return self._eval_sql(name, calc, context)
        if isinstance(calc, BatchCalculation):
            return self._eval_batch(name, calc, context)
        if isinstance(calc, DerivedCalculation):
            return self._eval_derived(name, calc, prior)
        raise MetricRuntimeError(f"Unsupported calculation for {name!r}: {type(calc)!r}")

    def _eval_formula(
        self,
        name: str,
        calc: FormulaCalculation,
        context: EvaluationContext,
    ) -> CalculationResult:
        executor = self._require_executor()
        filters = _string_filters(context.filters)
        try:
            value = executor.metric_value(
                calc.formula,
                at=None if context.window_start and context.window_end else context.effective_at,
                filters=filters or None,
                start=context.window_start,
                end=context.window_end,
            )
        except Exception as exc:  # noqa: BLE001
            return CalculationResult.from_error(name, str(exc), source="formula")
        return CalculationResult.from_value(name, float(value), source="formula")

    def _eval_sql(
        self,
        name: str,
        calc: SqlCalculation,
        context: EvaluationContext,
    ) -> CalculationResult:
        executor = self._require_sql_executor(calc.dialect)
        parameters = merge_parameter_bindings(context.bindings(), calc.bindings)
        try:
            value = executor.execute_scalar(
                calc.query,
                parameters,
                dialect=calc.dialect,
                value_column=calc.value_column,
            )
        except MetricRuntimeError as exc:
            return CalculationResult.from_error(name, str(exc), source="sql")
        except Exception as exc:  # noqa: BLE001
            return CalculationResult.from_error(name, str(exc), source="sql")
        if value is None:
            return CalculationResult.no_data(name, source="sql")
        return CalculationResult.from_value(name, float(value), source="sql")

    def _eval_batch(
        self,
        name: str,
        calc: BatchCalculation,
        context: EvaluationContext,
    ) -> CalculationResult:
        source_name = calc.source
        result_key = calc.result or name
        cache_key = (source_name, context.cache_key())
        if cache_key not in self._batch_cache:
            self._batch_cache[cache_key] = self._run_batch_source(source_name, context)
        row = self._batch_cache[cache_key]
        if result_key not in row:
            return CalculationResult.from_error(
                name,
                f"Batch source {source_name!r} did not return key {result_key!r}",
                source="batch",
            )
        value = row[result_key]
        if value is None:
            return CalculationResult.no_data(name, source="batch")
        return CalculationResult.from_value(name, float(value), source="batch")

    def _run_batch_source(
        self,
        source_name: str,
        context: EvaluationContext,
    ) -> dict[str, float | None]:
        source = self.batch_registry.get(source_name)
        if isinstance(source, SqlBatchSource):
            executor = self._require_sql_executor(source.dialect)
            parameters = merge_parameter_bindings(context.bindings(), source.bindings)
            row = executor.execute_named_row(
                source.query,
                parameters,
                dialect=source.dialect,
            )
            if source.columns:
                missing = [c for c in source.columns if c not in row]
                if missing:
                    raise MetricRuntimeError(
                        f"Batch source {source_name!r} missing columns: {missing}"
                    )
            return {
                str(k): (None if v is None else float(v))
                for k, v in row.items()
                if isinstance(v, (int, float, type(None))) and not isinstance(v, bool)
            }
        if isinstance(source, CallableBatchSource):
            return source.execute(context)
        return source.execute(context)

    def _eval_derived(
        self,
        name: str,
        calc: DerivedCalculation,
        prior: dict[str, CalculationResult],
    ) -> CalculationResult:
        ids = expression_identifiers(calc.expression)
        deps = {dep: prior[dep] for dep in ids if dep in prior}
        missing = ids - set(deps)
        if missing:
            return CalculationResult.from_error(
                name,
                f"Missing derived dependencies: {sorted(missing)}",
                source="derived",
            )
        result = evaluate_expression(calc.expression, deps)
        return CalculationResult(
            metric=name,
            status=result.status,
            value=result.value,
            error=result.error,
            source="derived",
        )

    def _calculation_for(self, name: str):
        metric = self.catalog.get(name)
        if metric.calculation is not None:
            return metric.calculation
        if metric.formula is not None:
            return FormulaCalculation(formula=metric.formula)
        raise MetricRuntimeError(f"Metric {name!r} has no calculation")

    def _dependency_closure(self, metric_names: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []

        def visit(name: str) -> None:
            if name in seen:
                return
            if name not in self.catalog:
                raise UnknownMetricError(f"Unknown metric: {name!r}")
            seen.add(name)
            metric = self.catalog.get(name)
            for dep in metric.dependencies:
                visit(dep)
            # Derived identifiers must also be closed even if omitted from deps
            # (catalog validation should already reject that case).
            calc = None
            try:
                calc = self._calculation_for(name)
            except MetricRuntimeError:
                calc = None
            if isinstance(calc, DerivedCalculation):
                for ident in expression_identifiers(calc.expression):
                    visit(ident)
            ordered.append(name)

        for name in metric_names:
            visit(name)
        return ordered

    def _topo_order(self, closure: Sequence[str]) -> list[str]:
        # Closure visit already deps-first; keep stable order.
        return list(closure)

    def _validate_catalog_calculations(self) -> None:
        for metric in self.catalog.values():
            calc = metric.calculation
            if calc is None and metric.formula is not None:
                calc = FormulaCalculation(formula=metric.formula)
            if isinstance(calc, DerivedCalculation):
                ids = expression_identifiers(calc.expression)
                declared = set(metric.dependencies)
                if not ids.issubset(declared):
                    raise InvalidMetricDefinitionError(
                        f"Metric {metric.id!r} derived expression references "
                        f"{sorted(ids - declared)} which are not declared in dependencies"
                    )
                for ident in ids:
                    if ident not in self.catalog:
                        raise UnknownMetricError(
                            f"Metric {metric.id!r} derived expression references "
                            f"unknown metric {ident!r}"
                        )
            if isinstance(calc, BatchCalculation) and calc.source not in self.batch_registry:
                # Registry may be populated after catalog construction in apps;
                # validate lazily at execution time. Still ensure result key shape.
                pass

    def _require_executor(self) -> Any:
        if self.executor is None:
            raise MetricRuntimeError("EvaluationSession has no executor for formula calculations")
        return self.executor

    def _require_sql_executor(self, dialect: str | None) -> Any:
        executor = self._require_executor()
        if (
            dialect is not None
            and self.sql_dialects is not None
            and dialect not in self.sql_dialects
        ):
            raise MetricRuntimeError(
                f"SQL dialect {dialect!r} is not supported by this executor "
                f"(supported: {sorted(self.sql_dialects)}). "
                "Metric Runtime does not transpile SQL across warehouses."
            )
        if not hasattr(executor, "execute_scalar"):
            raise MetricRuntimeError(
                f"Executor {type(executor).__name__} does not support SQL calculations"
            )
        # Also check executor-declared dialects if present.
        supported = getattr(executor, "sql_dialects", None)
        if dialect is not None and supported is not None and dialect not in supported:
            raise MetricRuntimeError(
                f"Executor does not support dialect {dialect!r} (supports {sorted(supported)})"
            )
        return executor

    @staticmethod
    def _to_observation(
        name: str,
        result: CalculationResult,
        context: EvaluationContext,
        *,
        definition_hash: str | None = None,
    ) -> KPIObservation:
        filters = {str(k): str(v) for k, v in context.filters.items() if v is not None}
        if result.status == ObservationValueStatus.VALUE and result.value is not None:
            return KPIObservation(
                name=name,
                value=float(result.value),
                as_of=context.effective_at,
                filters=filters,
                value_status=ObservationValueStatus.VALUE.value,
                metric_definition_hash=definition_hash,
            )
        return KPIObservation(
            name=name,
            value=0.0,
            as_of=context.effective_at,
            filters=filters,
            support_ok=False,
            value_status=result.status.value,
            calculation_error=result.error,
            metric_definition_hash=definition_hash,
        )

    def _observation_for(
        self,
        name: str,
        result: CalculationResult,
        context: EvaluationContext,
    ) -> KPIObservation:
        definition_hash = None
        try:
            definition_hash = self.catalog.get(name).semantic_hash()
        except Exception:  # noqa: BLE001
            definition_hash = None
        return self._to_observation(name, result, context, definition_hash=definition_hash)


def build_evaluation_context(
    *,
    at: datetime | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    filters: Mapping[str, ScalarValue] | dict[str, str] | None = None,
) -> EvaluationContext:
    """Build an EvaluationContext from engine-style at/start/end arguments."""
    raw_filters: dict[str, ScalarValue] = dict(filters or {})
    if start is not None and end is not None:
        return EvaluationContext(
            effective_at=ensure_utc(end),
            window_start=ensure_utc(start),
            window_end=ensure_utc(end),
            filters=raw_filters,
        )
    if at is None:
        raise MetricRuntimeError("EvaluationContext requires at= or start=/end=")
    return EvaluationContext(
        effective_at=ensure_utc(at),
        filters=raw_filters,
    )


def _string_filters(filters: Mapping[str, ScalarValue]) -> dict[str, str]:
    return {str(k): str(v) for k, v in filters.items() if v is not None}
