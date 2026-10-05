# Metric Runtime

A Python-native framework for defining, validating, executing and
operationalizing semantic business metrics.

> [!WARNING]
> metric-runtime is experimental.
> APIs may change substantially before 1.0.

## Runs where your metrics live

Metric Runtime runs against your existing data platform. Install the adapter
for that platform, read business facts from it, and write Metric Runtime
observations, state, incidents, and outbox records back to a dedicated
runtime namespace.

```
Your Data Platform
├── business data      → Metric Runtime reads
└── metric_runtime     ← Metric Runtime writes
```

```yaml
# connections.yaml — one connection, two logical roles
connections:
  warehouse:
    type: postgres
    dsn: ${DATABASE_URL}
    source_schema: analytics
    runtime_schema: metric_runtime

profiles:
  production:
    metric_source: warehouse
    runtime_store: warehouse
```

Source and runtime can also live on different platforms (e.g. a DuckDB or
warehouse source with a Postgres runtime store). Built-in adapters: `postgres`
(source + durable runtime store), `duckdb` (local source), `memory`
(development runtime store) and `webhook` (notifications). Other platforms
plug in as separate packages — see [docs/adapters.md](docs/adapters.md).

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
    Formula,
    DerivedCalculation,
    SeasonalZScore,
    Directionality,
)

revenue = Metric(
    id="revenue",
    name="Revenue",
    formula=Formula.sum("revenue"),
    unit="EUR",
    owner="finance",
)
profit = Metric(
    id="profit",
    name="Profit",
    formula=Formula.sum("profit"),
    unit="EUR",
    owner="finance",
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

catalog = MetricCatalog([revenue, profit, profit_margin])
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

In production each profile wires connections into three roles; each role is
resolved through the adapter registered for the connection's `type`:

```
metric-runtime run --profile production
        │
        ├── metric_source   business facts, read-only      (MetricExecutor)
        ├── runtime_store   observations, state, evaluations,
        │                   incidents, outbox               (RuntimeStore)
        └── notifier        outbox delivery                 (Notifier)
```

`MetricRuntime` schedules due windows and drains the outbox, `KPIEngine`
evaluates and commits; executors, runtime stores and notifiers come from
adapters. The core package assumes no database. `run --once` fits cron and
CronJobs; `run` is a long-lived worker.

More detail: [docs/architecture.md](docs/architecture.md),
[docs/adapters.md](docs/adapters.md) and [docs/production.md](docs/production.md).

## Examples

- `examples/company_metrics/` — Python metric repository
- `examples/sql_catalog/` — SQL / batch / derived calculations
- `examples/pypizza/` — flagship lunch-delivery scenario (Great Lunch talk)
- `examples/judgment_comparison/` — marimo demo: deterministic evidence → Jev / LLM typed judgment → Python policy
- `examples/metric_judgment_eval/` — Metric judgment evaluation — testing whether Jev/OpenAI add value only after Metric Runtime has exhausted deterministic graph analysis

### Jev vs LLM judgment comparison

A marimo application showing how deterministic Metric Runtime evidence can feed
the same typed business-decision contract through Jev and a general-purpose
LLM. Metric Runtime does not depend on either provider.

## Install

```bash
pip install metric-runtime
# plus the adapter for your platform, e.g.
pip install "metric-runtime[postgres]"   # Postgres source + durable runtime store
pip install "metric-runtime[duckdb]"     # local DuckDB source
```

## License

MIT

- Version: **0.2.0** (Alpha / Experimental)
