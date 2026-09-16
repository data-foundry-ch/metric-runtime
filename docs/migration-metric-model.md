# Migration: KPI → Metric

Metric Runtime 0.2 introduces `Metric` as the canonical semantic model.

## Before

```python
from metric_runtime import KPI, KPICatalog

profit_margin = KPI(
    name="profit_margin",
    label="Profit Margin",
    formula=...,
)
catalog = KPICatalog([profit_margin])
```

## After

```python
from metric_runtime import Metric, MetricCatalog

profit_margin = Metric(
    id="profit_margin",
    name="Profit Margin",
    calculation=...,
)
catalog = MetricCatalog([profit_margin])
```

## Field mapping

| Legacy KPI | Metric |
|---|---|
| `name` (machine id) | `id` |
| `label` (display) | `name` |
| `unit` string enum | `UnitSpec` (string still accepted) |
| — | `tags` |

## Compatibility

`KPI` and `KPICatalog` remain temporary aliases:

- `KPI(name="profit_margin", label="Profit Margin")` normalizes to
  `Metric(id="profit_margin", name="Profit Margin")`
- emits `DeprecationWarning`
- single internal representation: `Metric`

Legacy JSON with `"name": "profit_margin"` (id-shaped) still imports.

## When aliases may be removed

Before 1.0. Prefer migrating call sites now. Track removal in the changelog
when the project approaches a stable 1.0 API freeze.

## Runtime identity

Observations, `EvaluationKey`, state streams, incidents, and graph nodes use
**`Metric.id`**. Display UIs should show **`Metric.name`**.
