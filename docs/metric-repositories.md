# Metric repositories

A **metric repository** is a Git/Python project that defines typed semantic
metrics and composes them into a `MetricCatalog`.

Metric Runtime is both:

1. a **repository** for semantic metric definitions (DEFINE / VALIDATE)
2. a **runtime** that executes and operationalizes them (EXECUTE / OPERATE)

## What is a Metric?

A `Metric` is not merely a calculation. It carries:

- stable identity (`id`)
- human meaning (`name`, `description`)
- calculation (Formula / SQL / Batch / Derived)
- semantic dependencies
- dimensions
- unit
- ownership
- detector behavior

## Recommended structure

```
company-metrics/
├── metric-runtime.yaml
├── connections.example.yaml
├── metrics/
│   ├── __init__.py
│   ├── finance.py
│   ├── sales.py
│   └── catalog.py
└── tests/
```

Prefer an **explicit** repository contract: each module exports `METRICS`, and
`catalog.py` composes a `MetricCatalog`. Do not magically scan arbitrary modules.

## Defining a metric

```python
from metric_runtime import Metric, DerivedCalculation, SeasonalZScore

profit_margin = Metric(
    id="profit_margin",
    name="Profit Margin",
    description="Contribution profit as a percentage of revenue.",
    calculation=DerivedCalculation(expression="profit / revenue"),
    dependencies=["profit", "revenue"],
    dimensions=["country", "channel"],
    unit="percent",
    owner="finance",
    detector=SeasonalZScore(lookback_periods=6, threshold=3.0),
    tags={"finance", "executive"},
)
```

## Stable IDs and names

| Field | Role |
|---|---|
| `id` | Machine identity — dependencies, observations, state, incidents, diffs |
| `name` | Human display label |

Valid ids: `^[a-z][a-z0-9_]*$` (e.g. `profit_margin`, `closed_won_revenue`).

Changing display name from "Profit Margin" to "Contribution Margin %" must
**not** change `id`. Do not casually rename ids once runtime history exists.

Derived expressions reference **ids**, never display names:

```text
profit / revenue
```

## Building a catalog

```python
from metric_runtime import MetricCatalog
from .finance import METRICS as FINANCE_METRICS
from .sales import METRICS as SALES_METRICS

catalog = MetricCatalog([*FINANCE_METRICS, *SALES_METRICS], name="company")
```

## Validation

Construction validates. Explicitly:

```python
catalog.validate()
```

Rules include: unique ids, id pattern, known dependencies, no cycles, derived
expression ↔ dependency consistency, calculation/detector validity,
JSON-safe metadata.

Offline CLI (no warehouse I/O):

```bash
metric-runtime validate
```

## Dependencies

`Metric.dependencies` is the **business graph** (what explains this metric).
Batch sources are an **execution** optimization — a separate concern.

```python
catalog.dependencies("profit_margin")
catalog.ancestors("profit_margin")
catalog.subgraph("profit_margin")
catalog.topological_order()
```

## Units

Units are extensible `UnitSpec` values (descriptive only — no conversion algebra):

```python
Metric(..., unit="EUR")
Metric(..., unit=UnitSpec(id="milliseconds", symbol="ms"))
```

## Tags and metadata

Use `tags` for lightweight categorization and `metadata` for JSON-safe
extensions. Core semantic fields (`calculation`, `dependencies`, `owner`, …)
stay explicit — do not bury them in metadata.

Layout / draft / publish / permissions belong in a **product wrapper**, not on
`Metric`. See [product-integration.md](product-integration.md).

## Exporting JSON / JSONL

```python
catalog.to_json()
catalog.to_jsonl()
catalog.export_json("catalog.json")
MetricCatalog.from_json(...)
MetricCatalog.json_schema()
```

JSON is the **exchange** format. Python/Pydantic remains the **authoring** language.

## Catalog hashes

- `metric.semantic_hash()` — executable semantics (id, calculation, deps, unit,
  detector, directionality, support, impact, owner). Excludes display name,
  description, tags, format, metadata.
- `metric.content_hash()` — full document
- `catalog.semantic_hash()` — sorted composition of metric semantic hashes

## Catalog diffs

```python
diff = old_catalog.diff(new_catalog)
# diff.added / removed / changed[field_list]
```

## CI

```bash
pip install metric-runtime
metric-runtime validate
metric-runtime catalog export --format json --output catalog.json
```

See `examples/company_metrics/catalog-ci.example.yml`.

## Using the same catalog in runtime

```yaml
# metric-runtime.yaml
catalog:
  entrypoint: metrics.catalog:catalog
```

```python
engine = KPIEngine.from_profile("local")
```

## Using the same catalog in another product

Serialize `Metric` / `MetricCatalogSnapshot` and wrap with product fields.
Do not duplicate calculation/dependencies in a second schema.
