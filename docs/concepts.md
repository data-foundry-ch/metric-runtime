# Concepts

## Metrics aren't just numbers

A metric without context isn't intelligence. It's arithmetic.

metric-runtime explores KPIs as executable semantic objects that know:

- how they are calculated
- what they mean
- what they depend on
- which dimensions are valid
- what “unusual” means
- their current state
- who owns them
- how they participate in investigation and alerting

## Observation → Detection → State → Incident

| Layer | Question |
|---|---|
| Observation | What value did we measure? |
| Detection | Is it unusual? |
| State | Should the organization care yet? |
| Incident | Should somebody act? |

Detection asks whether something is unusual.
State determines whether the organization should care yet.

**An anomaly is not an alert.**

## Portable semantics

Independently versionable concerns:

1. **Business semantics** — KPI definitions, dependency graph, ownership, detectors
2. **Runtime policy** — evaluation cadence, state thresholds, investigation limits
3. **Infrastructure** — warehouse, lake, state database, credentials

Configuration mirrors that split:

- `metric-runtime.yaml` — project/runtime behavior (safe to commit)
- `connections.yaml` — environment resources + profiles (usually not committed)

If you use dbt, the separation should feel familiar: project semantics live with
the code; environment-specific connections live outside it.

metric-runtime does **not** require dbt and is **not** dbt-compatible.

## Graph-aware investigation

Don't poll the whole business. Propagate change through it.

Weekend Profit ↓
└── Profit Margin ↓
    ├── Average Order Value ↓
    │   └── Basket Threshold Concentration ↑
    └── Average Cost / Order ↑

Route to the team best positioned to act.

## Configuration precedence

1. Explicit Python arguments
2. Explicit CLI arguments
3. Selected runtime profile
4. `metric-runtime.yaml` project defaults
5. Library defaults

Environment variables resolve secret placeholders; they are not an uncontrolled
parallel configuration system.

## Agentic analytics (optional)

Executable semantics can provide a trusted reasoning substrate for analytical
agents — but that is **not** the main purpose of metric-runtime.

Don't make an LLM reconstruct your business from raw tables.
Make your business understandable to software.

An agent could call trusted operations such as:

- `get_kpi_status()`
- `get_dependencies()`
- `investigate_change()`
- `get_open_incidents()`
- `get_owner()`

Deterministic software should remain responsible for metric definitions,
calculations, dependency relationships, permissions, state, and business rules.

Agents may later help with orchestration, natural-language interaction,
explanation, and summarization.

Executable semantics turn the semantic layer from a dictionary into an API for
reasoning.
