# metric-runtime

Executable semantics for business metrics.

> [!WARNING]
> metric-runtime is experimental.
> APIs may change substantially before 1.0.

Dashboards calculate and display metrics.

**metric-runtime** explores what happens when metrics become executable
semantic objects with dependencies, detection strategies, state, ownership,
and graph-aware investigation.

Metrics aren't just numbers.
A metric without context isn't intelligence. It's arithmetic.

```python
from metric_runtime import (
    Formula,
    InMemoryStateStore,
    KPI,
    KPICatalog,
    KPIEngine,
    SeasonalZScore,
)
from metric_runtime.models import Directionality

profit_margin = KPI(
    name="profit_margin",
    owner="commercial-finance",
    formula=Formula.ratio("profit", "revenue"),
    dependencies=[
        "average_order_value",
        "average_cost_per_order",
    ],
    directionality=Directionality.LOWER_IS_BAD,
    detector=SeasonalZScore(lookback_periods=6, threshold=3.0),
)

catalog = KPICatalog([profit_margin])  # include dependency KPIs in real use
engine = KPIEngine(
    catalog=catalog,
    state_store=InMemoryStateStore(),
)
```

A metric-runtime KPI can know:

- what it means
- how it is calculated
- what explains it
- what dimensions are valid
- how abnormality is detected (serializable detector specs)
- who owns it
- its operational state

Detector policies are JSON-safe specs. The engine builds the runtime strategy:

```python
encoded = profit_margin.model_dump_json()
restored = KPI.model_validate_json(encoded)
assert restored == profit_margin
```

## Architecture

```
Metric semantics
      ↓
Observation
      ↓
Detection
      ↓
State
      ↓
Graph investigation
      ↓
Incident / Action
```

Detection asks whether something is unusual.
State determines whether the organization should care yet.

**An anomaly is not an alert.**

## Why metric-runtime?

- **Executable semantics** — KPIs are typed objects, not spreadsheet cells
- **Graph-aware investigation** — follow business dependencies, not only dashboards
- **Pluggable detection** — the z-score is one detector, not the architecture
- **Stateful metrics** — NORMAL → DETECTED → OPEN → …
- **Smart routing** — alert the owner best positioned to act

Don't poll the whole business. Propagate change through it.
Business semantics become software.

The authoritative loop is ``KPIEngine.process`` (alias ``tick``):

```python
result = engine.process(
    metric="profit_margin",
    at=timestamp,
    scope={"city": "Amsterdam"},
)
# result.transition == (previous_state, current_state)
# result.new_incidents / result.notifications only on meaningful changes
```

## Quick start

### A. Pure Python (embedding)

```bash
pip install -e ".[duckdb,dev]"
```

```python
from metric_runtime import Formula, InMemoryStateStore, KPI, KPICatalog, KPIEngine, SeasonalZScore
from metric_runtime.execution import DuckDBExecutor
from metric_runtime.models import Directionality

catalog = KPICatalog([
    KPI(
        name="requests",
        owner="growth",
        formula=Formula.sum("requests"),
        directionality=Directionality.TWO_SIDED,
    ),
    KPI(
        name="customers",
        owner="growth",
        formula=Formula.sum("customers"),
        dependencies=("requests",),
    ),
    KPI(
        name="conversion_rate",
        owner="growth",
        formula=Formula.ratio("customers", "requests"),
        dependencies=("customers", "requests"),
        directionality=Directionality.LOWER_IS_BAD,
        detector=SeasonalZScore(lookback_periods=6, threshold=3.0),
    ),
    KPI(
        name="recurring_revenue",
        owner="finance",
        formula=Formula.sum("recurring_revenue"),
        dependencies=("customers",),
        directionality=Directionality.LOWER_IS_BAD,
        detector=SeasonalZScore(lookback_periods=6, threshold=2.5),
    ),
])

engine = KPIEngine(
    catalog=catalog,
    executor=DuckDBExecutor("metrics.duckdb", fact_table="daily_metrics"),
    state_store=InMemoryStateStore(),
)
```

### B. Deployment configuration

If you use dbt, the separation should feel familiar: project semantics live
with the code; environment-specific connections live outside it.

| Concept | metric-runtime | analogous dbt idea |
|---|---|---|
| Project behavior | `metric-runtime.yaml` | `dbt_project.yml` |
| Environment resources | `connections.yaml` | `profiles.yml` |

metric-runtime does **not** require dbt and is **not** compatible with dbt configs.

```bash
metric-runtime validate \
  --project-config examples/pypizza/metric-runtime.yaml \
  --connections examples/pypizza/connections.yaml \
  --profile local

metric-runtime config show --profile local \
  --project-config examples/pypizza/metric-runtime.yaml \
  --connections examples/pypizza/connections.yaml

metric-runtime run --profile local \
  --project-config examples/pypizza/metric-runtime.yaml \
  --connections examples/pypizza/connections.yaml
```

The first style is convenient for embedding metric-runtime in Python applications.
The second is convenient for repeatable deployments.

## Core concepts

| Concept | Role |
|---|---|
| KPI | What the metric means |
| Executor | How to calculate it |
| Connection | Where the underlying data lives |
| Detector | Whether an observation is unusual |
| State store | What metric-runtime already concluded |
| Dependency graph | What explains this metric |
| Incident | Whether somebody should act |
| Notifier | How that action reaches them |

See [docs/concepts.md](docs/concepts.md) and [docs/architecture.md](docs/architecture.md).

## PyPizza example

The flagship example from the PyData talk
[*Your Dashboard Is Too Late*](docs/conference-talk.md):

```bash
pip install -e ".[demo,dev]"
python examples/pypizza/generate_data.py
marimo run examples/pypizza/app.py
```

Details: [examples/pypizza/README.md](examples/pypizza/README.md)

## Production considerations

v0.1 is an experimental library, not a full monitoring platform.

See [docs/production.md](docs/production.md).

## Project status

- Version: **0.1.0** (Alpha / Experimental)
- License: MIT
- Python: ≥ 3.11

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
