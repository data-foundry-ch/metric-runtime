# Metric Runtime

A Python-native framework for defining, validating, executing and
operationalizing semantic business metrics.

> [!WARNING]
> metric-runtime is experimental.
> APIs may change substantially before 1.0.

## Four capabilities

| | |
|---|---|
| **DEFINE** | Typed semantic metrics in Python/Pydantic |
| **VALIDATE** | `MetricCatalog` + semantic dependency graph |
| **EXECUTE** | Formula / SQL / Batch / Derived calculations |
| **OPERATE** | Detection → State → Investigation → Incident |

```python
from metric_runtime import (
    Metric,
    MetricCatalog,
    DerivedCalculation,
    SeasonalZScore,
    Directionality,
)

profit_margin = Metric(
    id="profit_margin",
    name="Profit Margin",
    description="Contribution profit as a percentage of revenue.",
    calculation=DerivedCalculation(expression="profit / revenue"),
    dependencies=["profit", "revenue"],
    dimensions=["country", "channel"],
    unit="percent",
    owner="finance",
    directionality=Directionality.LOWER_IS_BAD,
    detector=SeasonalZScore(lookback_periods=6, threshold=3.0),
)

catalog = MetricCatalog([profit_margin])  # include dependency metrics in real use
catalog.validate()
```

A `Metric` is not merely a calculation. It carries stable identity, human
meaning, calculation, dependencies, dimensions, unit, ownership, and detector
behavior. A Python package / Git repo becomes a **semantic metrics repository**.

`id` is machine identity. `name` is presentation. Renaming the display label
does not change history, dependencies, or runtime keys.

## Execute & operate

```
MetricCatalog
      ↓
EvaluationSession / calculations
      ↓
Observation
      ↓
Detection → State → Investigation → Incident / Outbox
```

Detection asks whether something is unusual.
State determines whether the organization should care yet.

**An anomaly is not an alert.**

## Building products on Metric Runtime

Metric Runtime owns semantic metric definitions.

Products may add draft/publish, organization scope, collections, permissions,
layout, and editors — prefer composition:

```python
class ProductMetric(BaseModel):
    metric: Metric
    lifecycle: Literal["draft", "published", "archived"]
```

rather than copying the semantic schema. See
[docs/product-integration.md](docs/product-integration.md).

## Why metric-runtime?

- **Metrics-as-code** — author catalogs in Python; exchange via JSON
- **Executable semantics** — typed objects, not spreadsheet cells
- **Graph-aware investigation** — follow business dependencies
- **Pluggable detection** — the z-score is one detector, not the architecture
- **Stateful metrics** — NORMAL → DETECTED → OPEN → …
- **Smart routing** — alert the owner best positioned to act

Don't poll the whole business. Propagate change through it.

## Calculations

See [docs/calculations.md](docs/calculations.md). Formula sugar still works:

```python
Metric(id="orders", name="Orders", formula=Formula.sum("orders"))
```

## Metric repositories

See [docs/metric-repositories.md](docs/metric-repositories.md) and
`examples/company_metrics/`.

```bash
metric-runtime validate
metric-runtime catalog list
metric-runtime catalog show profit_margin
metric-runtime catalog export --format json -o catalog.json
```

## Compatibility

`KPI` / `KPICatalog` remain temporary aliases for `Metric` / `MetricCatalog`.
See [docs/migration-metric-model.md](docs/migration-metric-model.md).

## Architecture

```
           Metric Repository
                   │
                   ▼
             MetricCatalog
       ┌───────────┴───────────┐
       │                       │
       ▼                       ▼
  Introspection             Runtime
  JSON / schema             Execution
  Docs / diffs              Detection
                            State
                            Investigation
```

More detail: [docs/architecture.md](docs/architecture.md).

## Examples

- `examples/company_metrics/` — Python metric repository
- `examples/sql_catalog/` — SQL / batch / derived calculations
- `examples/pypizza/` — flagship lunch-delivery scenario (Great Lunch talk)

## Install

```bash
pip install metric-runtime
# or with DuckDB backend:
pip install "metric-runtime[duckdb]"
```

## License

MIT

- Version: **0.2.0** (Alpha / Experimental)
