"""KPI catalog with early semantic validation."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping

import networkx as nx

from metric_runtime.exceptions import (
    DependencyCycleError,
    InvalidMetricDefinitionError,
    UnknownMetricError,
)
from metric_runtime.models import KPI


class KPICatalog:
    """Validated collection of KPI semantic definitions."""

    def __init__(self, metrics: Iterable[KPI] | Mapping[str, KPI] | None = None):
        if metrics is None:
            items: list[KPI] = []
        elif isinstance(metrics, Mapping):
            items = list(metrics.values())
        else:
            items = list(metrics)
        self._metrics = self._validate(items)

    @classmethod
    def from_dict(cls, data: Mapping[str, KPI]) -> KPICatalog:
        return cls(data)

    def _validate(self, items: list[KPI]) -> dict[str, KPI]:
        by_name: dict[str, KPI] = {}
        for metric in items:
            if not metric.name:
                raise InvalidMetricDefinitionError("KPI name must be non-empty")
            if metric.name in by_name:
                raise InvalidMetricDefinitionError(f"Duplicate KPI name: {metric.name!r}")
            by_name[metric.name] = metric

        for metric in by_name.values():
            for dep in metric.dependencies:
                if dep not in by_name:
                    raise UnknownMetricError(
                        f"KPI {metric.name!r} depends on unknown metric {dep!r}"
                    )

        graph = nx.DiGraph()
        for name, metric in by_name.items():
            graph.add_node(name)
            for dep in metric.dependencies:
                graph.add_edge(dep, name)
        if not nx.is_directed_acyclic_graph(graph):
            cycles = list(nx.simple_cycles(graph))
            raise DependencyCycleError(f"KPI dependency graph contains cycle(s): {cycles[:3]}")
        return by_name

    def get(self, name: str) -> KPI:
        try:
            return self._metrics[name]
        except KeyError as exc:
            raise UnknownMetricError(f"Unknown metric: {name!r}") from exc

    def __getitem__(self, name: str) -> KPI:
        return self.get(name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._metrics

    def __iter__(self) -> Iterator[str]:
        return iter(self._metrics)

    def __len__(self) -> int:
        return len(self._metrics)

    def keys(self):
        return self._metrics.keys()

    def values(self):
        return self._metrics.values()

    def items(self):
        return self._metrics.items()

    def as_dict(self) -> dict[str, KPI]:
        return dict(self._metrics)

    def names(self) -> list[str]:
        return list(self._metrics)
