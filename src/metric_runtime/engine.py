"""KPI evaluation engine.

Python API first. Configuration files for deployment.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from metric_runtime.catalog import KPICatalog
from metric_runtime.detectors import DetectorStrategy, SeasonalZScoreDetector
from metric_runtime.exceptions import MetricRuntimeError, UnknownMetricError
from metric_runtime.models import (
    KPI,
    DrilldownRow,
    KPIStatus,
    Measure,
)
from metric_runtime.notifications.base import Notifier, NullNotifier
from metric_runtime.stores.memory import InMemoryStateStore


class KPIEngine:
    """Evaluate KPIs, detect anomalies, and drive investigation."""

    def __init__(
        self,
        catalog: KPICatalog | dict[str, KPI] | list[KPI],
        executor=None,
        state_store=None,
        detector: DetectorStrategy | None = None,
        notifier: Notifier | None = None,
        *,
        # Back-compat: accept a raw DuckDB connection as first positional after catalog
        connection=None,
        fact_table: str = "pypizza_halfhourly",
    ):
        if isinstance(catalog, KPICatalog):
            self._catalog = catalog
        elif isinstance(catalog, dict):
            self._catalog = KPICatalog(catalog)
        else:
            self._catalog = KPICatalog(list(catalog))

        if connection is not None and executor is None:
            from metric_runtime.execution.duckdb import DuckDBExecutor

            executor = DuckDBExecutor(connection, fact_table=fact_table)

        self.executor = executor
        self.state_store = state_store or InMemoryStateStore()
        self.detector = detector or SeasonalZScoreDetector()
        self.notifier = notifier or NullNotifier()

        # Back-compat attributes used by quality helpers / demo code.
        self.fact_table = fact_table
        self.con = getattr(executor, "con", connection)

    @property
    def catalog(self) -> dict[str, KPI]:
        """Dict-like catalog for back-compat with demo/tests."""
        return self._catalog.as_dict()

    @property
    def catalog_dict(self) -> dict[str, KPI]:
        return self._catalog.as_dict()

    @property
    def kpi_catalog(self) -> KPICatalog:
        return self._catalog

    @classmethod
    def from_profile(
        cls,
        profile: str,
        *,
        project_config: str | Path | None = None,
        connections_config: str | Path | None = None,
        catalog: KPICatalog | dict[str, KPI] | list[KPI] | None = None,
    ) -> KPIEngine:
        from metric_runtime.config.factory import build_runtime

        return build_runtime(
            profile,
            project_config=project_config,
            connections_config=connections_config,
            catalog=catalog,
        )

    def _require_executor(self):
        if self.executor is None:
            raise MetricRuntimeError(
                "KPIEngine has no executor. Pass DuckDBExecutor(...) "
                "or build via KPIEngine.from_profile(...)."
            )
        return self.executor

    def _get_kpi(self, metric_name: str) -> KPI:
        try:
            return self._catalog.get(metric_name)
        except UnknownMetricError:
            raise

    def metric_value(
        self,
        metric_name: str,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float:
        metric = self._get_kpi(metric_name)
        if metric.formula is None:
            raise MetricRuntimeError(f"KPI {metric_name!r} has no formula for execution")
        return self._require_executor().metric_value(
            metric.formula, at=at, filters=filters, start=start, end=end
        )

    def measure_value(
        self,
        measure: Measure,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float:
        return self._require_executor().measure_value(
            measure, at=at, filters=filters, start=start, end=end
        )

    def support_value(
        self,
        metric_name: str,
        at: datetime | None = None,
        filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> float:
        metric = self._get_kpi(metric_name)
        if metric.support is None:
            return self.measure_value(Measure.ORDERS, at, filters, start, end)
        return self.measure_value(metric.support.measure, at, filters, start, end)

    def baseline_values(
        self,
        metric_name: str,
        at: datetime,
        filters: dict[str, str] | None = None,
    ) -> list[float]:
        cfg = self._get_kpi(metric_name).detector
        return [
            self.metric_value(metric_name, at - timedelta(weeks=i), filters)
            for i in range(1, cfg.baseline_weeks + 1)
        ]

    def evaluate(
        self,
        metric_name: str,
        at: datetime,
        filters: dict[str, str] | None = None,
    ) -> KPIStatus:
        metric = self._get_kpi(metric_name)
        current = self.metric_value(metric_name, at, filters)
        baseline = self.baseline_values(metric_name, at, filters)
        support = self.support_value(metric_name, at, filters)
        support_ok = True
        if metric.support is not None:
            support_ok = support >= metric.support.minimum

        return self.detector.evaluate(
            metric_name,
            current,
            baseline,
            directionality=metric.directionality,
            config=metric.detector,
            support=support,
            support_ok=support_ok,
            as_of=at.isoformat(sep=" "),
        )

    def evaluate_window(
        self,
        metric_name: str,
        start: datetime,
        end: datetime,
        filters: dict[str, str] | None = None,
        baseline_weeks: int = 6,
    ) -> KPIStatus:
        """Compare a multi-interval window to same windows in prior weeks."""
        metric = self._get_kpi(metric_name)
        current = self.metric_value(metric_name, filters=filters, start=start, end=end)
        baseline = []
        for i in range(1, baseline_weeks + 1):
            baseline.append(
                self.metric_value(
                    metric_name,
                    filters=filters,
                    start=start - timedelta(weeks=i),
                    end=end - timedelta(weeks=i),
                )
            )
        support = self.support_value(metric_name, filters=filters, start=start, end=end)
        support_ok = True
        if metric.support is not None:
            support_ok = support >= metric.support.minimum

        return self.detector.evaluate(
            metric_name,
            current,
            baseline,
            directionality=metric.directionality,
            config=metric.detector,
            support=support,
            support_ok=support_ok,
            as_of=f"{start.isoformat(sep=' ')} → {end.isoformat(sep=' ')}",
        )

    def estimate_impact_eur(
        self,
        metric_name: str,
        at: datetime,
        current_value: float,
        baseline_value: float,
        filters: dict[str, str] | None = None,
    ) -> float:
        metric = self._get_kpi(metric_name)
        kind = metric.impact.kind

        if kind == "none":
            return 0.0
        if kind == "margin_delta":
            return max(0.0, baseline_value - current_value)
        if kind == "revenue_delta":
            return max(0.0, baseline_value - current_value)
        if kind == "cost_delta":
            return max(0.0, current_value - baseline_value)
        if kind == "orders_delta":
            delta = baseline_value - current_value
            if delta <= 0:
                return 0.0
            if "average_order_value" in self._catalog:
                aov = self.metric_value("average_order_value", at, filters)
                return delta * aov
            return delta
        return 0.0

    def drilldown(
        self,
        metric_name: str,
        at: datetime,
        dimensions: Iterable[str],
        only_anomalies: bool = True,
        parent_filters: dict[str, str] | None = None,
    ) -> list[DrilldownRow]:
        dims = tuple(dimensions)
        metric = self._get_kpi(metric_name)
        executor = self._require_executor()
        allowed = set(metric.dimensions) | getattr(executor, "valid_dimensions", set())
        if not set(dims).issubset(allowed):
            raise ValueError(f"{metric_name} only supports dimensions {metric.dimensions}")

        rows: list[DrilldownRow] = []
        for filters in executor.distinct_groups(dims, at=at, filters=parent_filters):
            status = self.evaluate(metric_name, at, filters)
            if only_anomalies and not status.anomaly:
                continue
            impact_eur = self.estimate_impact_eur(
                metric_name,
                at,
                status.value,
                status.baseline_mean,
                filters,
            )
            rows.append(
                DrilldownRow(
                    filters=filters,
                    value=status.value,
                    baseline_mean=status.baseline_mean,
                    relative_change=status.relative_change,
                    z_score=status.z_score,
                    anomaly=status.anomaly,
                    support=status.support,
                    impact_eur=impact_eur,
                )
            )
        rows.sort(key=lambda row: row.impact_eur, reverse=True)
        return rows

    def investigate(
        self,
        metric: str,
        at: datetime | None = None,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        filters: dict[str, str] | None = None,
        preferred_leaves: tuple[str, ...] = (),
    ):
        from metric_runtime.investigation import investigate_metric, investigate_window

        if start is not None and end is not None:
            return investigate_window(
                self,
                metric,
                start,
                end,
                filters,
                preferred_leaves=preferred_leaves,
            )
        if at is None:
            raise MetricRuntimeError("investigate() requires at= or start=/end=")
        return investigate_metric(self, metric, at, filters, preferred_leaves=preferred_leaves)

    def nearby_events(
        self,
        at: datetime,
        filters: dict[str, str] | None = None,
        hours: int = 48,
    ) -> list[dict[str, object]]:
        executor = self._require_executor()
        if hasattr(executor, "nearby_events"):
            return executor.nearby_events(at, filters=filters, hours=hours)
        return []

    def basket_distribution(
        self,
        start: datetime,
        end: datetime,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        executor = self._require_executor()
        if hasattr(executor, "basket_distribution"):
            return executor.basket_distribution(start, end, filters=filters)
        raise MetricRuntimeError("Current executor does not support basket_distribution()")
