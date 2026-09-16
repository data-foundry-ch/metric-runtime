"""KPI catalog with early semantic validation."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

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

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """JSON Schema for a catalog document (array of KPI objects)."""
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "KPICatalog",
            "type": "array",
            "items": KPI.model_json_schema(),
        }

    def to_list(self) -> list[dict[str, Any]]:
        """Stable JSON-serializable list of KPI dicts (calculations included)."""
        return [
            metric.model_dump(mode="json")
            for metric in sorted(self._metrics.values(), key=lambda m: m.name)
        ]

    def to_json(self, *, indent: int | None = 2) -> str:
        """Export catalog as a JSON array."""
        return json.dumps(self.to_list(), indent=indent)

    def to_jsonl(self) -> str:
        """Export catalog as JSON Lines (one KPI per line, sorted by name)."""
        lines = [
            json.dumps(metric.model_dump(mode="json"), separators=(",", ":"))
            for metric in sorted(self._metrics.values(), key=lambda m: m.name)
        ]
        return "\n".join(lines) + ("\n" if lines else "")

    @classmethod
    def from_json(cls, text: str) -> KPICatalog:
        """Import catalog from a JSON array of KPI objects."""
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise InvalidMetricDefinitionError("Catalog JSON must be an array of KPI objects")
        return cls([KPI.model_validate(item) for item in payload])

    @classmethod
    def from_jsonl(cls, text: str) -> KPICatalog:
        """Import catalog from JSON Lines (one KPI object per non-empty line)."""
        items: list[KPI] = []
        for line_no, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                items.append(KPI.model_validate_json(line))
            except Exception as exc:  # noqa: BLE001
                raise InvalidMetricDefinitionError(
                    f"Invalid KPI on JSONL line {line_no}: {exc}"
                ) from exc
        return cls(items)

    def write_json(self, path: str | Path, *, indent: int | None = 2) -> None:
        Path(path).write_text(self.to_json(indent=indent), encoding="utf-8")

    def write_jsonl(self, path: str | Path) -> None:
        Path(path).write_text(self.to_jsonl(), encoding="utf-8")

    @classmethod
    def read_json(cls, path: str | Path) -> KPICatalog:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def read_jsonl(cls, path: str | Path) -> KPICatalog:
        return cls.from_jsonl(Path(path).read_text(encoding="utf-8"))

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

        self._validate_derived_calculations(by_name)
        return by_name

    @staticmethod
    def _validate_derived_calculations(by_name: dict[str, KPI]) -> None:
        from metric_runtime.calculations.expressions import expression_identifiers
        from metric_runtime.calculations.specs import DerivedCalculation, FormulaCalculation

        for metric in by_name.values():
            calc = metric.calculation
            if calc is None and metric.formula is not None:
                calc = FormulaCalculation(formula=metric.formula)
            if not isinstance(calc, DerivedCalculation):
                continue
            ids = expression_identifiers(calc.expression)
            declared = set(metric.dependencies)
            if not ids.issubset(declared):
                raise InvalidMetricDefinitionError(
                    f"KPI {metric.name!r} derived expression references "
                    f"{sorted(ids - declared)} which are not declared in dependencies"
                )
            for ident in ids:
                if ident not in by_name:
                    raise UnknownMetricError(
                        f"KPI {metric.name!r} derived expression references "
                        f"unknown metric {ident!r}"
                    )

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
