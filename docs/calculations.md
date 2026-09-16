# Calculations

Metric Runtime distinguishes **what a KPI means** from **how its value is obtained**.

```
KPI semantics (owner, dependencies, detector, …)
        │
        └── calculation
                ├── FormulaCalculation
                ├── SqlCalculation
                ├── BatchCalculation
                └── DerivedCalculation
                        ↓
              EvaluationSession
                        ↓
                 KPIObservation
                        ↓
            existing runtime lifecycle
            (detector → state → investigation → incident → outbox)
```

## Why calculations are first-class

Real catalogs mix:

- simple fact-table aggregates
- warehouse-native SQL
- expensive shared queries that feed many KPIs
- ratios/attainment metrics derived from other KPIs

These are **execution concerns**. They must not leak Salesforce/Snowflake/schema
details into the semantic KPI object, and they must not bypass the existing
idempotent/ordered state runtime.

## Formula calculations

**Use when** the KPI is a SUM / ratio / difference over known measures in a
fact table (PyPizza style).

```python
KPI(
    name="orders",
    calculation=FormulaCalculation(formula=Formula.sum("orders")),
)
```

Legacy sugar still works in 0.1.x:

```python
KPI(name="orders", formula=Formula.sum("orders"))
# normalizes to FormulaCalculation
```

**Limitations:** no joins, CTEs, or multi-table logic — use SQL or Batch.

## SQL calculations

**Use when** the KPI needs arbitrary relational logic.

```python
KPI(
    name="closed_won_revenue",
    calculation=SqlCalculation(
        dialect="duckdb",
        query="""
            SELECT SUM(amount) AS value
            FROM opportunity
            WHERE is_won
              AND close_date = :effective_at
              AND team = :team
        """,
    ),
)
```

### Contract

- Query is **parameterized** (`:name` bindings)
- Returns **exactly one row**
- Must include a ``value`` column (or `value_column=...`)
- `NULL` → `NO_DATA` (not silently `0`)
- Zero rows → `NO_DATA`
- Multiple rows / missing column / SQL errors → explicit `ERROR`

### KPI-local bindings

`SqlCalculation.bindings` supply KPI-local defaults (e.g. thresholds like
`aging_days`). They must be JSON-serializable scalars
(`str|int|float|bool|None|datetime`) — never credentials.

```python
SqlCalculation(
    dialect="duckdb",
    query="""
        SELECT COUNT(*) AS value
        FROM opportunity
        WHERE age_days >= :aging_days
          AND close_date = :effective_at
    """,
    bindings={"aging_days": 30},
)
```

**Precedence:** `EvaluationContext` bindings (window + filters + aliases)
override KPI defaults on key conflicts. KPI bindings only fill missing keys.

The same merge rule applies to `SqlBatchSource.bindings`.

### Dialect

`dialect="duckdb"` means: this query is intended for a DuckDB-capable executor.

Metric Runtime does **not** transpile DuckDB SQL to Snowflake/BigQuery.
A dialect mismatch fails clearly.

DuckDB may be used as a **SQL/batch-only** executor without a designated
`fact_table`. Formula / measure aggregation still requires an explicit
fact table.

## Batch calculations

**Use when** many KPIs can share one expensive query.

Batch is an **execution optimization**, not a business dependency.

```python
registry.register(
    "sales_metrics",
    SqlBatchSource("sales_metrics", query="SELECT ... AS closed_won_revenue, ...", dialect="duckdb"),
)

KPI(name="closed_won_revenue", calculation=BatchCalculation(source="sales_metrics", result="closed_won_revenue"))
KPI(name="slipped_value", calculation=BatchCalculation(source="sales_metrics", result="slipped_value"))
```

Within one `EvaluationSession`, each unique `(source, EvaluationContext)` runs
**once**. Results are fanned out to referencing KPIs.

Application code owns the registry. Core never hardcodes domain batch names.
Python handlers may be registered via `CallableBatchSource` (not from YAML).

## Derived calculations

**Use when** the metric is computed from already-evaluated KPIs.

```python
KPI(
    name="revenue_attainment",
    dependencies=("closed_won_revenue", "bookings_target"),
    calculation=DerivedCalculation(
        expression="closed_won_revenue / bookings_target",
    ),
)
```

### Rules

- Expression identifiers must be a **subset of declared dependencies**
- Safe AST whitelist only (`+ - * /`, names, numbers, `min`/`max`/`coalesce`/`nullif`)
- No attribute access, imports, comprehensions, or arbitrary calls
- Division by zero / missing deps → `NO_DATA` (does not crash the catalog)

## Evaluation context

```python
EvaluationContext(
    effective_at=...,          # business time
    window_start=...,          # optional
    window_end=...,            # optional
    filters={"team": "enterprise"},
)
```

Logical bindings include:

| Binding | Meaning |
|---|---|
| `effective_at` | Business evaluation time (canonical) |
| `at` / `as_of_date` | Aliases of `effective_at` (product filter convenience) |
| `window_start` / `start_date` | Window start (defaults to `effective_at`) |
| `window_end` / `end_date` | Window end (defaults to `effective_at`) |
| filter keys | From `EvaluationContext.filters` |

`as_of_date` does **not** change `effective_at` semantics — it is the same
datetime under another name for product SQL that already uses that parameter.

Executors bind parameters safely — never string-concatenate filter values into
SQL.

## Display metadata (embedders)

First-class display fields on `KPI` (not layout):

| Field | Purpose |
|---|---|
| `label` | Human-readable name (`display_name` falls back to title-cased `name`) |
| `unit` | `count` / `ratio` / `eur` / `percent` / `unit` |
| `format` | Optional format hint for UI bridges (e.g. `0.0%`, `#,##0`) |

Use `kpi.display()` for `{label, unit, format}`. Keep layout
(parent / ring / order) in `metadata["presentation"]` — product concern, not
core semantics.

## Presentation threshold bands

Optional **presentation policy** only — not detectors, not alerts, not colors:

```python
from metric_runtime import (
    PresentationThreshold,
    classify_presentation_band,
    Directionality,
)

band = classify_presentation_band(
    observation.measured_value,
    thresholds=PresentationThreshold(target=100, warning=90, critical=70),
    directionality=Directionality.LOWER_IS_BAD,
)
# on_target | at_risk | off_target | no_data
```

Detectors and operational state remain authoritative for ops. Bands are for
product UI bridges that need target / warning / critical presentation.

## Observation null semantics

`KPIObservation.value_status` is the source of truth for embedders:

| `value_status` | Meaning | `value` field | Prefer |
|---|---|---|---|
| `value` | Real measurement (including legitimate `0.0`) | measured number | `measured_value` |
| `no_data` | NULL / zero rows / missing deps | placeholder `0.0` | `measured_value is None` |
| `error` | Calculation failed (`calculation_error`) | placeholder `0.0` | `measured_value is None` |

Do **not** treat placeholder `0.0` as a measured zero when status is not
`value`. Use `has_value` / `measured_value` / `is_no_data` / `is_error`.

## Catalog interchange

Minimal export/import (calculations included) — not a draft/publish server:

```python
schema = KPICatalog.json_schema()          # JSON Schema (array of KPI)
text = catalog.to_json()                   # JSON array
catalog = KPICatalog.from_json(text)
lines = catalog.to_jsonl()                 # one KPI per line
catalog = KPICatalog.from_jsonl(lines)
catalog.write_jsonl("catalog.jsonl")
catalog = KPICatalog.read_jsonl("catalog.jsonl")
```

Round-trips Formula / SQL / Batch / Derived calculations via Pydantic.

## Evaluation session

`EvaluationSession` coordinates calculation only:

1. dependency closure
2. topological order
3. shared batch deduplication
4. formula / SQL / derived execution
5. `KPIObservation` output

It does **not** run detectors, state machines, incidents, or notifications.

## Connections

| Lives with the metric definition | Lives with the environment |
|---|---|
| SQL text | passwords / DSNs |
| `dialect="duckdb"` | `connections.yaml` profile |
| batch source names | warehouse host |

## Runtime integration

```
Calculation → KPIObservation → Detection → State → Investigation → Incident → Outbox
```

`KPIEngine.metric_value` / `evaluate` / `process` continue to own the lifecycle.
Calculation feeds observations into that path; it does not replace it.

## Business graph vs execution graph

**Business / semantic graph** (`KPI.dependencies`):

> What explains this KPI?

**Execution grouping** (shared batch sources):

> How can these values be calculated efficiently?

Do not merge these graphs. Two KPIs may share a batch source without depending
on each other semantically.
