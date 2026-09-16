"""Metric catalog — the in-memory semantic metric repository."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import networkx as nx
from pydantic import BaseModel, Field

from metric_runtime.exceptions import (
    DependencyCycleError,
    InvalidMetricDefinitionError,
    UnknownDependencyError,
    UnknownMetricError,
)
from metric_runtime.models import Metric


class MetricCatalogSnapshot(BaseModel):
    """Deterministic serializable catalog document."""

    schema_version: str = "1"
    catalog_name: str | None = None
    metrics: list[Metric] = Field(default_factory=list)


class MetricCatalogDiff(BaseModel):
    """Structural catalog comparison (not a migration engine)."""

    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    changed: dict[str, list[str]] = Field(default_factory=dict)


class MetricCatalog:
    """Validated collection of Metric semantic definitions.

    Construction always validates. Use :meth:`validate` for an explicit
    re-check after external mutation is not supported — catalogs are immutable
    after build.
    """

    def __init__(
        self,
        metrics: Iterable[Metric] | Mapping[str, Metric] | None = None,
        *,
        name: str | None = None,
    ):
        if metrics is None:
            items: list[Metric] = []
        elif isinstance(metrics, Mapping):
            items = list(metrics.values())
        else:
            items = list(metrics)
        self.name = name
        self._metrics = self._validate(items)
        self._graph = self._build_graph(self._metrics)

    @classmethod
    def from_dict(cls, data: Mapping[str, Metric], *, name: str | None = None) -> MetricCatalog:
        return cls(data, name=name)

    def validate(self) -> MetricCatalog:
        """Re-run catalog validation (returns self). Construction already validates."""
        self._metrics = self._validate(list(self._metrics.values()))
        self._graph = self._build_graph(self._metrics)
        return self

    @staticmethod
    def _build_graph(by_id: dict[str, Metric]) -> nx.DiGraph:
        graph = nx.DiGraph()
        for metric_id, metric in by_id.items():
            graph.add_node(
                metric_id,
                name=metric.name,
                label=metric.display_name,
                owner=metric.owner,
                description=metric.description,
                unit=metric.unit.model_dump(mode="json"),
                directionality=metric.directionality.value,
            )
        for metric_id, metric in by_id.items():
            for dep in metric.dependencies:
                if dep in by_id:
                    graph.add_edge(dep, metric_id)
        return graph

    def _validate(self, items: list[Metric]) -> dict[str, Metric]:
        by_id: dict[str, Metric] = {}
        for metric in items:
            if not isinstance(metric, Metric):
                raise InvalidMetricDefinitionError(
                    f"Catalog entries must be Metric instances, got {type(metric)!r}"
                )
            if metric.id in by_id:
                raise InvalidMetricDefinitionError(
                    f"Duplicate Metric.id: {metric.id!r}. "
                    "Catalog construction never silently overwrites metrics."
                )
            by_id[metric.id] = metric

        for metric in by_id.values():
            for dep in metric.dependencies:
                if dep not in by_id:
                    suggestion = _suggest_id(dep, by_id.keys())
                    hint = f" Did you mean {suggestion!r}?" if suggestion else ""
                    raise UnknownDependencyError(
                        f'Metric "{metric.id}" depends on unknown metric "{dep}".{hint}'
                    )

        graph = nx.DiGraph()
        for metric_id, metric in by_id.items():
            graph.add_node(metric_id)
            for dep in metric.dependencies:
                graph.add_edge(dep, metric_id)
        if not nx.is_directed_acyclic_graph(graph):
            cycles = list(nx.simple_cycles(graph))
            raise DependencyCycleError(f"Metric dependency graph contains cycle(s): {cycles[:3]}")

        self._validate_derived_calculations(by_id)
        return by_id

    @staticmethod
    def _validate_derived_calculations(by_id: dict[str, Metric]) -> None:
        from metric_runtime.calculations.expressions import expression_identifiers
        from metric_runtime.calculations.specs import DerivedCalculation, FormulaCalculation

        for metric in by_id.values():
            calc = metric.calculation
            if calc is None and metric.formula is not None:
                calc = FormulaCalculation(formula=metric.formula)
            if not isinstance(calc, DerivedCalculation):
                continue
            ids = expression_identifiers(calc.expression)
            declared = set(metric.dependencies)
            if not ids.issubset(declared):
                raise InvalidMetricDefinitionError(
                    f"Metric {metric.id!r} derived expression references "
                    f"{sorted(ids - declared)} which are not declared in dependencies"
                )
            for ident in ids:
                if ident not in by_id:
                    raise UnknownMetricError(
                        f"Metric {metric.id!r} derived expression references "
                        f"unknown metric {ident!r}"
                    )

    def get(self, metric_id: str) -> Metric:
        try:
            return self._metrics[metric_id]
        except KeyError as exc:
            raise UnknownMetricError(f"Unknown metric: {metric_id!r}") from exc

    def __getitem__(self, metric_id: str) -> Metric:
        return self.get(metric_id)

    def __contains__(self, metric_id: object) -> bool:
        return isinstance(metric_id, str) and metric_id in self._metrics

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

    def as_dict(self) -> dict[str, Metric]:
        return dict(self._metrics)

    def names(self) -> list[str]:
        """Return metric ids (historical method name)."""
        return list(self._metrics)

    def ids(self) -> list[str]:
        return list(self._metrics)

    def graph(self) -> nx.DiGraph:
        return self._graph.copy()

    def dependencies(self, metric_id: str) -> list[str]:
        self.get(metric_id)
        return list(self._graph.predecessors(metric_id))

    def dependents(self, metric_id: str) -> list[str]:
        self.get(metric_id)
        return list(self._graph.successors(metric_id))

    def ancestors(self, metric_id: str) -> list[str]:
        self.get(metric_id)
        return sorted(nx.ancestors(self._graph, metric_id))

    def descendants(self, metric_id: str) -> list[str]:
        self.get(metric_id)
        return sorted(nx.descendants(self._graph, metric_id))

    def topological_order(self) -> list[str]:
        return list(nx.topological_sort(self._graph))

    def subgraph(self, metric_id: str) -> MetricCatalog:
        """Catalog of ``metric_id`` plus all ancestors (dependency closure)."""
        self.get(metric_id)
        nodes = {metric_id, *nx.ancestors(self._graph, metric_id)}
        return MetricCatalog([self._metrics[n] for n in sorted(nodes)], name=self.name)

    def to_snapshot(self) -> MetricCatalogSnapshot:
        metrics = [self._metrics[mid] for mid in sorted(self._metrics)]
        return MetricCatalogSnapshot(
            schema_version="1",
            catalog_name=self.name,
            metrics=metrics,
        )

    @classmethod
    def from_snapshot(cls, snapshot: MetricCatalogSnapshot | dict[str, Any]) -> MetricCatalog:
        if isinstance(snapshot, dict):
            snapshot = MetricCatalogSnapshot.model_validate(snapshot)
        return cls(snapshot.metrics, name=snapshot.catalog_name)

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """JSON Schema for a catalog snapshot document."""
        return MetricCatalogSnapshot.model_json_schema()

    def to_list(self) -> list[dict[str, Any]]:
        return [m.model_dump(mode="json") for m in self.to_snapshot().metrics]

    def to_json(self, *, indent: int | None = 2) -> str:
        return self.to_snapshot().model_dump_json(indent=indent)

    def to_jsonl(self) -> str:
        lines = [
            json.dumps(m.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
            for m in self.to_snapshot().metrics
        ]
        return "\n".join(lines) + ("\n" if lines else "")

    @classmethod
    def from_json(cls, text: str) -> MetricCatalog:
        payload = json.loads(text)
        if isinstance(payload, dict) and "metrics" in payload:
            return cls.from_snapshot(payload)
        if isinstance(payload, list):
            return cls([Metric.model_validate(item) for item in payload])
        raise InvalidMetricDefinitionError(
            "Catalog JSON must be a snapshot object or an array of Metric objects"
        )

    @classmethod
    def from_jsonl(cls, text: str) -> MetricCatalog:
        items: list[Metric] = []
        for line_no, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                items.append(Metric.model_validate_json(line))
            except Exception as exc:  # noqa: BLE001
                raise InvalidMetricDefinitionError(
                    f"Invalid Metric on JSONL line {line_no}: {exc}"
                ) from exc
        return cls(items)

    def write_json(self, path: str | Path, *, indent: int | None = 2) -> None:
        Path(path).write_text(self.to_json(indent=indent), encoding="utf-8")

    def write_jsonl(self, path: str | Path) -> None:
        Path(path).write_text(self.to_jsonl(), encoding="utf-8")

    def export_json(self, path: str | Path, *, indent: int | None = 2) -> None:
        self.write_json(path, indent=indent)

    @classmethod
    def read_json(cls, path: str | Path) -> MetricCatalog:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def read_jsonl(cls, path: str | Path) -> MetricCatalog:
        return cls.from_jsonl(Path(path).read_text(encoding="utf-8"))

    def semantic_hash(self) -> str:
        """SHA-256 over sorted per-metric semantic hashes."""
        parts = [f"{mid}:{self._metrics[mid].semantic_hash()}" for mid in sorted(self._metrics)]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def content_hash(self) -> str:
        parts = [f"{mid}:{self._metrics[mid].content_hash()}" for mid in sorted(self._metrics)]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def diff(self, other: MetricCatalog) -> MetricCatalogDiff:
        """Compare this catalog (old) to ``other`` (new)."""
        old_ids = set(self._metrics)
        new_ids = set(other._metrics)
        added = sorted(new_ids - old_ids)
        removed = sorted(old_ids - new_ids)
        changed: dict[str, list[str]] = {}
        for mid in sorted(old_ids & new_ids):
            left = self._metrics[mid].model_dump(mode="json")
            right = other._metrics[mid].model_dump(mode="json")
            fields = sorted(k for k in set(left) | set(right) if left.get(k) != right.get(k))
            if fields:
                changed[mid] = fields
        return MetricCatalogDiff(added=added, removed=removed, changed=changed)

    def to_markdown(self) -> str:
        """Simple deterministic Markdown documentation for the catalog."""
        lines = ["# Metric catalog", ""]
        if self.name:
            lines.extend([f"Catalog: **{self.name}**", ""])
        for metric in self.to_snapshot().metrics:
            lines.append(f"## {metric.name}")
            lines.append("")
            lines.append(f"- ID: `{metric.id}`")
            lines.append(f"- Unit: `{metric.unit.id}`")
            if metric.owner:
                lines.append(f"- Owner: {metric.owner}")
            calc = metric.calculation
            kind = getattr(calc, "kind", None) or "unknown"
            lines.append(f"- Calculation: {kind}")
            if metric.description:
                lines.append("")
                lines.append(metric.description)
            if metric.dependencies:
                lines.append("")
                lines.append("Dependencies:")
                for dep in metric.dependencies:
                    lines.append(f"- `{dep}`")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _suggest_id(unknown: str, known: Iterable[str], *, limit: int = 1) -> str | None:
    """Tiny edit-distance suggestion for unknown dependency errors."""
    unknown_l = unknown.lower()
    scored: list[tuple[int, str]] = []
    for candidate in known:
        if abs(len(candidate) - len(unknown)) > 3:
            continue
        dist = _levenshtein(unknown_l, candidate.lower())
        if dist <= 2:
            scored.append((dist, candidate))
    if not scored:
        return None
    scored.sort()
    return scored[0][1]


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            ins = curr[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            curr.append(min(ins, delete, sub))
        prev = curr
    return prev[-1]


# Temporary compatibility alias.
KPICatalog = MetricCatalog

__all__ = [
    "KPICatalog",
    "MetricCatalog",
    "MetricCatalogDiff",
    "MetricCatalogSnapshot",
]
